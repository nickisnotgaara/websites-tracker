# Merge firecrawl + monitoring into single Python app

**Date:** 2026-08-28
**Status:** Approved (awaiting user spec review)
**Owner:** Mavis (assistant) + user

## Context

`website-tracker` currently has two apps:

- `monitoring/` — Python (aiogram + firecrawl-py SDK + DeepSeek + Google Sheets), runs as `competitor_tracker` container
- `firecrawl/` — full upstream firecrawl monorepo (Node.js, 1.3GB image), self-hosted on port 3002

The monitoring app only uses one firecrawl endpoint — `POST /v2/map` (and `/v2/scrape` for sample pages). Running a 1.3GB Node.js stack for a single sitemap-discovery call is wasteful, and the two-service architecture makes deploy brittle (we already hit issues with port conflicts, stale networks, and auth mismatches).

This refactor merges everything into one Python app that:
- Inlines the `/v2/map` and `/v2/scrape` logic in pure Python
- Drops the firecrawl-py SDK and the separate firecrawl service entirely
- Packages into a single Docker image (uv builder + python:3.12-slim runtime)
- Runs locally for dev (`uv run main.py`) and as a single container in prod

## Goals

1. **One app, one image** — single Dockerfile, single container, no `docker-compose.yml`
2. **Drop the firecrawl dependency entirely** — remove `firecrawl-py`, remove `firecrawl/` directory
3. **Preserve all existing behavior** — bot, scheduler, AI analysis, Google Sheets logging, dedup of known URLs, sample-clustering
4. **Same external contract** — `.env`, `credentials.json`, `data/tracker.db`, Telegram bot token all unchanged
5. **Run dev locally with `uv run`** and deploy with `docker run`

## Non-goals

- Switching to a different AI provider (DeepSeek stays)
- Changing Telegram bot UX
- Adding tests for the entire app (only the new discovery modules get pytest tests)
- Re-architecting storage (sqlitedict stays)
- CI/CD setup
- Performance optimization (current scale is fine)

## Architecture

```
website-tracker/
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-08-28-merge-monitoring-firecrawl-design.md   (this file)
├── monitoring/                                ← SINGLE PYTHON APP
│   ├── Dockerfile                              (new: multi-stage uv + python:3.12-slim)
│   ├── pyproject.toml                          (deps updated: -firecrawl-py +httpx +trafilatura +selectolax +pytest)
│   ├── main.py                                 (unchanged)
│   ├── .env                                    (unchanged)
│   ├── credentials.json                        (unchanged, read-only mount)
│   ├── data/                                   (preserved, contains tracker.db)
│   ├── logs/                                   (preserved, contains app.log)
│   ├── tests/
│   │   ├── __init__.py
│   │   └── test_discovery.py                   (new: ~80 lines pytest)
│   ├── README.md                               (updated: dev/deploy commands)
│   └── src/
│       ├── __init__.py
│       ├── config.py                           (unchanged)
│       ├── storage.py                          (unchanged)
│       └── services/
│           ├── __init__.py
│           ├── monitor.py                      (CHANGED: drop firecrawl_service calls → discovery.*)
│           ├── ai.py                           (unchanged)
│           ├── gsheets.py                      (unchanged)
│           ├── bot/                            (unchanged)
│           │   ├── __init__.py
│           │   ├── main.py
│           │   └── keyboards.py
│           └── discovery/                      (NEW MODULE)
│               ├── __init__.py                 (re-exports: discover_urls, scrape_markdown)
│               ├── sitemap.py                  (~120 lines: /v2/map equivalent)
│               └── scrape.py                   (~30 lines: /v2/scrape equivalent)
└── firecrawl/                                 ← DELETED ENTIRELY
    (no longer needed)
```

**Runtime layout (single container):**

```
┌─────────────────────────────────────────────────┐
│  Docker image: competitor-monitor:latest        │
│  base: python:3.12-slim                        │
│  ENTRYPOINT: ["python", "main.py"]              │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │  main.py                                 │  │
│  │  ├─ bot polling (aiogram)                 │  │
│  │  └─ scheduler (apscheduler, daily 08:00) │  │
│  │                                           │  │
│  │  services/                                │  │
│  │  ├─ monitor.py  → orchestrate              │  │
│  │  ├─ discovery.sitemap.discover_urls(...)   │  │
│  │  ├─ discovery.scrape.scrape_markdown(...)  │  │
│  │  ├─ ai.analyze_updates_batch(...)         │  │
│  │  ├─ gsheets.log_new_collection(...)       │  │
│  │  └─ bot.broadcast_notification(...)        │  │
│  │                                           │  │
│  │  storage (sqlitedict) + config (pydantic) │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  volumes: data/  logs/                          │
│  env files: .env  credentials.json (ro)         │
└─────────────────────────────────────────────────┘
```

## Components

### A. `src/services/discovery/sitemap.py` (NEW, ~120 lines)

Inline replacement for `POST /v2/map`. Stateless, sync function. Faithful to firecrawl behavior.

**Public API:**

```python
def discover_urls(domain_url: str, *, timeout: float = 15.0, max_urls: int = 500) -> list[str]:
    """Discover URLs on a site via sitemap (and link fallback).

    Returns deduplicated, same-domain URLs only.
    Never raises — returns [] on any error, logging via loguru.
    """
```

**Algorithm (1-to-1 with firecrawl `/v2/map`):**

1. Try `GET {domain}/robots.txt` → extract `Sitemap: <url>` line(s) (one per line).
2. Build list of candidate sitemap URLs:
   - From `robots.txt` (if found)
   - Plus `{domain}/sitemap.xml`
   - Plus `{domain}/sitemap_index.xml`
3. For each candidate URL (in order, dedup):
   - `GET` with 15s timeout, 1 retry on 5xx/timeout
   - Parse XML
   - If `<sitemapindex>`: extract `<sitemap><loc>` entries → recurse (BFS, depth limit 3)
   - Else: extract `<urlset><url><loc>` entries → add to result set
4. If no sitemap returned any URLs:
   - Link discovery fallback: `GET {domain}/`, parse `<a href="...">`, resolve relative, filter same-domain (urllib.parse.urlparse + hostname compare), take first 50
5. Dedupe (preserve order via `dict.fromkeys`)
6. Filter to same-domain (in case sitemap leaked cross-domain)
7. Cap to `max_urls` (default 500) — log warning if truncated
8. Return `list(result_set)`

**Error handling:**
- Any HTTP/parse/timeout error on a candidate → skip it, continue with next
- Total function never raises
- Logs at WARNING for each skipped source, INFO for final count

**Key implementation choices:**
- `httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "competitor-monitor/1.0"})` (one client per call, not pooled — simplicity > speed at this scale)
- `xml.etree.ElementTree` for parsing (stdlib, no dep)
- Namespace handling: register namespaces `{"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}` and use them, fallback to no-namespace

### B. `src/services/discovery/scrape.py` (NEW, ~30 lines)

Inline replacement for `POST /v2/scrape` (markdown format).

**Public API:**

```python
def scrape_markdown(url: str, *, timeout: float = 30.0, max_chars: int = 50_000) -> str:
    """Fetch URL and return article content as markdown.

    Returns "" on any error, logging via loguru.
    Truncates output to max_chars.
    """
```

**Algorithm:**

1. `httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": "competitor-monitor/1.0"})`
2. Detect content-type; reject non-HTML (return `""`)
3. `trafilatura.extract(response.text, output_format="markdown", include_links=False, include_images=False, include_tables=True, favor_precision=True)`
4. If extract returns `None` or empty → return `""`
5. Truncate to `max_chars` (cut at last newline before limit)
6. Return the markdown string

### C. `src/services/discovery/__init__.py` (NEW, ~10 lines)

```python
from .sitemap import discover_urls
from .scrape import scrape_markdown

__all__ = ["discover_urls", "scrape_markdown"]
```

### D. `src/services/monitor.py` (CHANGED, ~5 line diff)

Two call sites change:

**Before:**
```python
import asyncio
from src.services.firecrawl import firecrawl_service

curr_urls_raw = await asyncio.to_thread(
    firecrawl_service.map_site, domain_url
)
...
content = await asyncio.to_thread(firecrawl_service.scrape_url, url)
```

**After:**
```python
import asyncio
from src.services import discovery

curr_urls_raw = await asyncio.to_thread(
    discovery.discover_urls, domain_url
)
...
content = await asyncio.to_thread(discovery.scrape_markdown, url)
```

**File deletion:** `src/services/firecrawl.py` is removed entirely.

### E. `pyproject.toml` (CHANGED)

**Remove:**
```toml
"firecrawl-py>=1.0.0",
```

**Add:**
```toml
"httpx>=0.27.0",
"trafilatura>=2.0.0",
"selectolax>=0.3.0",
```

**Add `[dependency-groups]`:**
```toml
[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]
```

### F. `Dockerfile` (NEW, multi-stage, ~25 lines)

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY main.py ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && uv pip install --system --no-cache .

# ─── runtime ───
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    IN_DOCKER=true

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/src ./src
COPY --from=builder /build/main.py ./
COPY pyproject.toml ./

RUN mkdir -p data logs

CMD ["python", "main.py"]
```

### G. `docker-compose.yml` (DELETED)

Single container, no external services → compose is unnecessary. Documented in README how to run.

### H. `README.md` (UPDATED, ~30 line diff)

Replace existing README with:

```markdown
# Competitor Monitor

Telegram bot + scheduler that monitors competitor bridal-shop websites for new
collection pages, analyzes changes with DeepSeek, and logs findings to Google
Sheets.

## What it does

1. Cron at 08:00 Moscow time (or manual "🚀 Начать парсинг" in Telegram) triggers a check
2. For each competitor URL in the Google Sheet "Отслеживаемые конкуренты":
   - Sitemap discovery (`/sitemap.xml`, `/robots.txt` → sitemap, link fallback)
   - Diff vs known URLs in `data/tracker.db`
   - For new URLs: cluster + sample → scrape markdown → DeepSeek analysis
3. Sends summary to all admin users via Telegram
4. Logs new collections to Google Sheet "Новинки у конкурентов"

## Local dev

```bash
cd monitoring
uv sync
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN, SPREADSHEET_ID, AI_API_KEY
# credentials.json from Google Cloud service account goes in this dir
uv run main.py
```

## Tests

```bash
uv run pytest tests/
```

## Docker deploy

```bash
cd monitoring
docker build -t competitor-monitor .
docker run -d --name competitor-monitor \
  --restart unless-stopped \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/credentials.json:/app/credentials.json:ro \
  competitor-monitor
```

## Logs

`logs/app.log` is rotated daily by loguru. View with `tail -f logs/app.log`.

## Data

`data/tracker.db` (sqlitedict) — single source of truth for known URLs per competitor.
Safe to delete for a clean reset; bot will re-initialize on next run.
```

## Data flow

```
┌──────────────────┐  cron 08:00 MSK  ┌─────────────────────────────┐
│ apscheduler      ├────────────────►│  monitor.run_check_cycle()  │
│ (main.py)        │                 │  (services/monitor.py)      │
└──────────────────┘                 └────────────┬────────────────┘
                                                │
        ┌───────────────────────────────────────┼────────────────────────────────┐
        │                                       │                                │
        ▼                                       ▼                                ▼
┌──────────────────┐              ┌──────────────────────────┐    ┌────────────────────────┐
│ gsheets          │              │ discovery.discover_urls() │    │ ai.analyze_batch()      │
│ .get_competitors │              │   → list[str]            │    │   (DeepSeek)             │
│ .log_new_collect │              │ (sitemap + link fallback) │    │   → summary text         │
└──────────────────┘              └──────────────────────────┘    └────────────────────────┘
                                                │
                                                ▼
                                  ┌────────────────────────────┐
                                  │ discovery.scrape_markdown()│
                                  │   for sampled URLs only     │
                                  │   → "markdown string"       │
                                  └────────────────────────────┘
                                                │
        ┌───────────────────────────────────────┼────────────────────────────────┐
        ▼                                       ▼                                ▼
┌──────────────────┐              ┌──────────────────────────┐    ┌────────────────────────┐
│ storage          │              │ bot.broadcast_notif()    │    │ storage.update_known() │
│ (sqlitedict)     │              │  (Telegram)              │    │                        │
└──────────────────┘              └──────────────────────────┘    └────────────────────────┘
```

**Step-by-step (one competitor URL):**

1. `competitors = gsheets.get_competitors()` → `[url1, url2, ...]`
2. For each `url`:
   a. `known = storage.get_known_urls(url)` (set of strings, empty if first run)
   b. `current = await discovery.discover_urls(url)` (set of strings from sitemap/link)
   c. If `current` empty → log warning, skip this competitor
   d. If `known` empty → `storage.update_known_urls(url, current)` (init), skip
   e. `new = current - known`
   f. If `new` empty → log "no new pages", skip
   g. `unique = deduplicate_urls(new)` (canonical-ize multilingual)
   h. `samples, clusters = cluster_and_sample(unique)` (top 5 clusters, 1 URL each)
   i. For each `sample_url`:
      - `markdown = await discovery.scrape_markdown(sample_url)` (may be empty)
      - Collect `(sample_url, markdown)` tuples
   j. `summary = await ai.analyze_updates_batch(samples, clusters)` (DeepSeek)
   k. `await bot.broadcast_notification(formatted_text)`
   l. `await gsheets.log_new_collection(url, summary, all_new_urls)`
   m. `storage.update_known_urls(url, new)` (save all detected new URLs)

## Error handling

| Component | Error | Behavior |
|---|---|---|
| `discovery.discover_urls` | sitemap 404/5xx/parse error | Skip source, try next, log warning |
| `discovery.discover_urls` | all sources fail | Return `[]`, log error |
| `discovery.discover_urls` | link discovery fails | Log warning, return `[]` |
| `discovery.scrape_markdown` | timeout/404/non-HTML | Return `""`, log warning |
| `ai.analyze_updates_batch` | API error | Propagate exception (caught by `monitor.run_check_cycle` top-level) |
| `gsheets.log_new_collection` | API error | Propagate (caught by top-level) |
| `bot.broadcast_notification` | API error | Log error, continue (don't fail the whole cycle) |
| `storage.update_known_urls` | DB error | Propagate (catch at top-level → broadcast failure to user) |

**Top-level error handling in `run_check_cycle()`** (existing, unchanged):
```python
async def run_check_cycle():
    try:
        competitors = await asyncio.to_thread(gsheets_service.get_competitors)
        if not competitors:
            logger.warning("No competitors found in Google Sheets!")
            return
        for domain in competitors:
            try:
                await self.check_competitor(domain)
            except Exception as e:
                logger.error(f"check_competitor failed for {domain}: {e}")
                continue
    except Exception as e:
        logger.exception(f"run_check_cycle failed: {e}")
        raise
```

## Testing

`pytest` covers only the new modules (sitemap, scrape). Existing modules are not tested (out of scope for this refactor).

**`tests/test_discovery.py` (~80 lines):**

```python
import pytest
from src.services.discovery.sitemap import discover_urls, _parse_sitemap_xml, _extract_robots_sitemaps
from src.services.discovery.scrape import scrape_markdown

# --- sitemap ---

def test_extract_robots_sitemaps_simple():
    text = "User-agent: *\nDisallow: /admin\nSitemap: https://example.com/sitemap.xml\n"
    assert _extract_robots_sitemaps(text) == ["https://example.com/sitemap.xml"]

def test_extract_robots_sitemaps_multiple():
    text = "Sitemap: https://a.com/sitemap.xml\nSitemap: https://b.com/sitemap.xml\n"
    assert _extract_robots_sitemaps(text) == [
        "https://a.com/sitemap.xml", "https://b.com/sitemap.xml"
    ]

def test_parse_sitemap_xml_urlset():
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/b</loc></url>
    </urlset>"""
    assert _parse_sitemap_xml(xml) == ["https://example.com/a", "https://example.com/b"]

def test_parse_sitemap_xml_index():
    xml = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/s1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/s2.xml</loc></sitemap>
    </sitemapindex>"""
    assert _parse_sitemap_xml(xml) == [
        "https://example.com/s1.xml", "https://example.com/s2.xml"
    ]

# Integration test (marked slow, uses real httpbin.org if available)
@pytest.mark.slow
def test_discover_urls_real_site():
    urls = discover_urls("https://httpbin.org", timeout=10)
    assert isinstance(urls, list)

# --- scrape ---

def test_scrape_markdown_returns_str():
    # Use a tiny test fixture or mock httpx
    result = scrape_markdown("https://httpbin.org/html", timeout=10)
    assert isinstance(result, str)
```

**Run:** `uv run pytest tests/`

CI not configured (YAGNI for freelance scope). Tests are run manually before each deploy.

## Migration plan

1. **Backup** `data/tracker.db` and `data/` (just in case)
2. **Create new files** in parallel (don't delete old yet):
   - `src/services/discovery/__init__.py`
   - `src/services/discovery/sitemap.py`
   - `src/services/discovery/scrape.py`
   - `tests/__init__.py`
   - `tests/test_discovery.py`
3. **Update** `pyproject.toml` (deps)
4. **Update** `src/services/monitor.py` (replace 2 call sites)
5. **Update** `Dockerfile` (new multi-stage)
6. **Add** `docs/superpowers/specs/...` and `README.md`
7. **Delete** `src/services/firecrawl.py`, `firecrawl/` directory, `docker-compose.yml`
8. **Test locally**:
   - `uv sync`
   - `uv run pytest tests/`
   - `uv run main.py` — verify it starts, bot responds, scheduler set
   - Trigger manual parse via Telegram "🚀 Начать парсинг" against 1 competitor
9. **Build & test Docker**:
   - `docker build -t competitor-monitor .` — must complete without error
   - `docker run -d --name competitor-monitor-test ...` — verify it starts
   - `docker logs -f competitor-monitor-test` — verify no traceback
   - Trigger another manual parse via Telegram
10. **Replace old container**:
    - `docker stop competitor_tracker && docker rm competitor_tracker`
    - `docker run -d --name competitor-monitor ...` (production)
11. **Stop & remove** leftover firecrawl artifacts:
    - `docker stop firecrawl-api-1 firecrawl-nuq-postgres-1 firecrawl-rabbitmq-1 firecrawl-playwright-service-1 firecrawl-redis-1`
    - `docker rm ...`
    - `docker network rm firecrawl_backend 2>/dev/null || true`
    - `docker image rm firecrawl-api firecrawl-nuq-postgres firecrawl-playwright-service rabbitmq:3-management 2>/dev/null || true` (optional, frees ~3GB disk)
12. **Update** `tracker.db` not needed — schema is unchanged (sqlitedict, no migration)
13. **Document** in README and add a note in CHANGELOG-style log: "2026-08-28: firecrawl dependency removed, single-container architecture"

## Acceptance criteria

- [ ] `uv run main.py` starts the bot and scheduler without errors
- [ ] `uv run pytest tests/` — all tests pass
- [ ] `docker build -t competitor-monitor .` completes (image < 400 MB)
- [ ] `docker run` (production command) starts the container, bot responds to `/start`
- [ ] Manual parse via "🚀 Начать парсинг" successfully:
  - Discovers URLs from a real competitor (sitemap or link fallback)
  - Scrapes sample pages to markdown
  - Sends DeepSeek-analyzed summary to Telegram
  - Logs to Google Sheet
- [ ] No `firecrawl-*` containers or images in `docker ps -a` / `docker images`
- [ ] No `firecrawl-py` import anywhere in the source (grep test)
- [ ] `data/tracker.db` schema unchanged (existing entries still load)
- [ ] `.env` schema unchanged (existing config still works)
- [ ] Image size drops from ~1.5 GB (firecrawl + monitoring) to ~400 MB (monitoring alone)

## Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `trafilatura` extraction quality differs from firecrawl | Medium | Medium (AI gets noisier input) | Run manual parse on 2-3 real competitors before declaring done; compare output to old scrape; tune `favor_precision` flag if needed |
| Sitemap recursion depth wrong for large sites | Low | Low (depth limit 3 + max_urls 500) | Make `max_depth` and `max_urls` parameters, tune per logs |
| `uv export --frozen` fails on missing lockfile | Medium | Medium (build fails) | Verify `uv.lock` exists in current `monitoring/`; if not, run `uv lock` first |
| Old firecrawl-py still in lockfile after `uv lock` | Low | Low (extra dep) | Manually verify `pyproject.toml` and `uv.lock` after edit |
| Existing `competitor_tracker` container conflicts on port | None | None | Old container uses no host ports (Python only connects to firecrawl) |
| Google Sheets API quota | Low | Medium | Same as before — no change |
| Telegram bot token invalid after restart | None | None | No change |

## Open questions

None — all design decisions made.

## References

- `monitoring/src/services/firecrawl.py` (current SDK wrapper, to be deleted)
- `monitoring/src/services/monitor.py` (orchestration, to be updated)
- `monitoring/Dockerfile` (current simple Dockerfile, to be replaced)
- `monitoring/pyproject.toml` (current deps, to be updated)
- `firecrawl/apps/api/src/controllers/v2/map.ts` (reference for sitemap logic — to be re-implemented in Python)
- `firecrawl/apps/api/src/services/sitemap.ts` (reference for link discovery — to be re-implemented in Python)
- Previous session memory: `~/.minimax/agents/mavis/memory/MEMORY.md` entry "Website-tracker: local firecrawl + monitoring (2026-08-28)"
