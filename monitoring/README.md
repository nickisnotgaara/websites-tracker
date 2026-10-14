# Competitor Monitor

Telegram bot + scheduler that monitors competitor bridal-shop websites for new
collection pages, analyzes changes with DeepSeek, and logs findings to Google
Sheets.

## What it does

1. The schedule (default: 08:00 Moscow time **daily**) — or the manual
   "🚀 Начать парсинг" button in Telegram — triggers a check. Set `SCHEDULE_DAYS`
   to pick the weekdays, e.g. `mon,thu` for twice a week.
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

During a run the bot mirrors that log into a **self-updating Telegram message**
sent to **every admin account**, for both triggers (the "🚀 Начать парсинг"
button and the configured schedule). One run, one message per chat; the run
owns the log, admins are subscribers — so a scheduled run is no longer silent.

Each message carries a progress header (`3/9`, elapsed, ✅/❌ counters), the
trigger that started the run, a **summary of reasons** grouped by cause
(`403 — блокируют бота — 45: …`), a `✅ / ❌ / ⏳ / ⏸️` line per competitor with the
failure reason, a "Падают подряд" section for sites that failed 3+ runs in a row,
and the tail of the log lines. The message is rewritten with `editMessageText`,
so no new message is created per update.

### Full failure report

One 4096-char message cannot hold 50+ failures and stay readable, so when the
run finishes the bot sends **follow-up messages listing every failed site** —
grouped by reason, every domain named, `⚠️` lines for sites that keep failing.
A clean run sends nothing extra. A pathological run (200+ failures) falls back
to one line per site split across as many messages as needed.

The invariant the tests enforce: **no failed site disappears from the output**.
If the main message had to cut failures, it says so explicitly
(`… ещё 238 магазинов — полный список в отчёте об ошибках`) instead of the old
`ошибки — вверху`, which was literally untrue when the cut landed on a failure.

Report messages include only `WARNING`/`ERROR` log lines — the `INFO` stream was
30% of the old message's budget and said nothing.

Telegram allows ~1 message/second per chat, so edits are throttled and identical
renders are never sent. Each chat is throttled and error-handled independently:
if one admin chat hits a 429 or becomes uneditable, the rest keep receiving
updates. A shop that fails no longer aborts the run — the cycle reports it as
`❌` with a reason and moves on.

The message text is rendered **once** per tick and the same string is pushed to
all chats, so N admins cost one render and N throttled edits.

### Failure reasons

A site with `0 URLs discovered` is not a mystery any more: `discover_urls_detailed()`
returns a machine code and a Russian explanation, which is what the log line and
the Telegram row show.

| Code | Meaning |
|---|---|
| `dns` | домен не резолвится — сайта больше нет |
| `refused` | сервер не отвечает |
| `tls` | ошибка TLS/сертификата |
| `timeout` | таймаут ответа |
| `http_403` | 403 — блокируют бота |
| `http_404` | 404 — не найдено |
| `http_429` | 429 — нас ограничивают |
| `http_5xx` | 5xx на сайте |
| `no_sitemap` | sitemap не найден |
| `empty_page` | главная без ссылок (JS-сайт) |
| `challenge` | JS-защита (заглушка) |
| `no_html` | не HTML |

`exception` (неожиданная ошибка, текст в логе) добавляется в `monitor.py`, а
`truncated` — это флаг на успешном результате: найдено больше 500 URL, список
обрезан, в сообщении видно «⚠️ много URL».

Проверено на живых сайтах: несуществующий домен → `dns`, `weddings.com` →
`http_403`, `weddingsdress.com` → `challenge`, `instagram.com/*` → `empty_page`,
`davidsbridal.com` → 131 748 URL обрезано до 500 с `truncated=True`.

`discover_urls()` и `discover_urls_detailed()` — обычные функции (не корутины).

Consecutive failures are tracked per domain under the `shop_health` key in
`data/tracker.db` (written once per run). Nothing is auto-deleted and no
User-Agent is rotated: a dead domain is reported, the decision is yours.

Column F ("парсится ли?") of the competitors sheet is **display-only** — the mark
is shown as ⛔ in the log, but marked sites are still parsed.

Tunable via `.env` (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_LIVE_LOG` | `true` | Master switch; `false` restores the old behaviour |
| `TELEGRAM_LIVE_LOG_INTERVAL` | `3.0` | Seconds between edit attempts |
| `TELEGRAM_LIVE_LOG_LEVEL` | `INFO` | loguru level mirrored into Telegram |
| `TELEGRAM_LIVE_LOG_MIN_GAP` | `1.05` | Hard floor between two API calls to the same chat |
| `TELEGRAM_LIVE_LOG_BUFFER` | `2000` | In-memory log ring buffer size |

### Schedule

The schedule lives in `.env` and needs a bot restart to take effect:

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULE_DAYS` | `*` | `mon,thu` for twice a week, `mon,tue,wed,thu,fri` for weekdays, `*` daily |
| `SCHEDULE_HOUR` | `8` | Hour in `SCHEDULE_TZ` |
| `SCHEDULE_MINUTE` | `0` | Minute |
| `SCHEDULE_TZ` | `Europe/Moscow` | IANA timezone |

The bot logs what it scheduled (`Scheduler started. Next run: пн, чт в
08:00`). An unknown day name is rejected at startup with a message naming the
valid values, instead of silently falling back to daily.

### "No new items" is not an error

A site that was checked successfully and simply has nothing new is a different
outcome from a site that could not be checked, and the report says so
explicitly:

```
✅ quiet-shop.com — проверен · новых нет      ← reachable, nothing new
❌ blocked-shop.com — 403 — блокируют бота    ← could not be checked
```

A clean run with zero new items states `🆕 нет новых` in the header instead of
showing a bare count. The failure report header separates the two as well
(`✅ Проверено успешно: 2 (из них без новинок: 1, с новинками: 1)`), and the
list below it contains failures only.

## Data

`data/tracker.db` (sqlitedict) — single source of truth for known URLs per
competitor. Safe to delete for a clean reset; the bot will re-initialize on
the next run.

Keys: `competitors` (URLs per site), `users` (admin chat IDs — the live log goes
to all of them), `app_config`, `shop_health` (failure streaks, one write per run).
