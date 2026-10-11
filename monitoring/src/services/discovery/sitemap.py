"""Inline sitemap + link discovery, replacing firecrawl /v2/map."""
import re
import xml.etree.ElementTree as ET
import httpx
from selectolax.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from loguru import logger
from typing import Final

_SITEMAP_NS: Final = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_SITEMAP_RE: Final = re.compile(
    r"^\s*Sitemap\s*:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)


@dataclass
class FetchResult:
    """Outcome of a single HTTP GET, including *why* it failed."""

    response: httpx.Response | None = None
    code: str | None = None  # dns | timeout | refused | tls | http_5xx | unknown
    detail: str = ""


@dataclass
class DiscoveryResult:
    """Discovery outcome for one domain. `code` is the failure taxonomy."""

    urls: list[str] = field(default_factory=list)
    ok: bool = False
    code: str = "ok"
    http_status: int | None = None
    truncated: bool = False
    detail: str = ""


# Human-readable Russian reasons, short enough for one report line.
_REASONS: Final = {
    "dns": "домен не резолвится",
    "timeout": "таймаут ответа",
    "refused": "сервер не отвечает",
    "tls": "ошибка TLS/сертификата",
    "http_403": "403 — блокируют бота",
    "http_404": "404 — не найдено",
    "http_429": "429 — нас ограничивают",
    "http_5xx": "5xx на сайте",
    "no_sitemap": "sitemap не найден",
    "empty_page": "главная без ссылок (JS-сайт)",
    "challenge": "JS-защита (заглушка)",
    "no_html": "не HTML",
}

_CHALLENGE_MAX_BYTES = 1024


def _reason(code: str) -> str:
    return _REASONS.get(code, code)


def _status_reason(status: int | None) -> tuple[str, str]:
    """Map an HTTP status to (code, human detail)."""
    if status is None:
        return "unknown", _REASONS["dns"]
    if status == 403:
        return "http_403", _REASONS["http_403"]
    if status == 404:
        return "http_404", _REASONS["http_404"]
    if status == 429:
        return "http_429", _REASONS["http_429"]
    if status >= 500:
        return "http_5xx", _REASONS["http_5xx"]
    return "http_error", f"HTTP {status}"


def _classify_transport_error(exc: Exception) -> tuple[str, str]:
    """Tell DNS failure apart from a refused connection and a TLS problem.

    httpx collapses all three into ConnectError, so the message is the only
    signal available -- and it is a reliable one on Windows/POSIX alike.
    """
    text = str(exc).lower()
    if "getaddrinfo" in text or "name or service not known" in text or "11001" in text:
        return "dns", _REASONS["dns"]
    if "certificate" in text or "ssl" in text or "tls" in text:
        return "tls", _REASONS["tls"]
    if "timed out" in text or "timeout" in text:
        return "timeout", _REASONS["timeout"]
    if "refused" in text or "reset" in text or "unreachable" in text:
        return "refused", _REASONS["refused"]
    return "refused", _REASONS["refused"]


def _extract_robots_sitemaps(robots_txt: str) -> list[str]:
    """Extract `Sitemap:` URLs from a robots.txt body.

    Case-insensitive (per the spec). Returns absolute URLs only,
    preserves order, dedupes via dict.fromkeys.
    Never raises — returns [] on any error.
    """
    try:
        matches = _SITEMAP_RE.findall(robots_txt)
    except Exception as e:
        logger.warning(f"Failed to parse robots.txt: {e}")
        return []
    return list(dict.fromkeys(m.strip() for m in matches))


def _parse_sitemap_xml(xml_bytes: bytes) -> list[str]:
    """Parse a sitemap XML body and return URLs.

    Handles two shapes:
    - <urlset><url><loc>...</loc></url>...</urlset> → page URLs
    - <sitemapindex><sitemap><loc>...</loc></sitemap>...</sitemapindex>
      → sitemap URLs (orchestrator recurses into these)

    Returns empty list on parse error. Never raises.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning(f"Failed to parse sitemap XML: {e}")
        return []
    except Exception as e:
        logger.warning(f"Unexpected error parsing sitemap XML: {e}")
        return []

    urls: list[str] = []
    # sitemapindex (nested sitemaps) — recurse target list
    for sitemap in root.findall(f"{_SITEMAP_NS}sitemap"):
        loc = sitemap.find(f"{_SITEMAP_NS}loc")
        if loc is not None and loc.text:
            urls.append(loc.text.strip())
    # urlset (page URLs)
    for url in root.findall(f"{_SITEMAP_NS}url"):
        loc = url.find(f"{_SITEMAP_NS}loc")
        if loc is not None and loc.text:
            urls.append(loc.text.strip())
    return urls


_MAX_RECURSION_DEPTH = 3
_LINK_FALLBACK_LIMIT = 50
_DEFAULT_HEADERS = {"User-Agent": "competitor-monitor/1.0"}


def _is_same_domain(url: str, base_domain: str) -> bool:
    """Return True if url's netloc matches base_domain's netloc (modulo default port and www.)."""
    try:
        def _normalize_netloc(s: str) -> str:
            parsed = urlparse(s)
            nl = parsed.netloc.lower()
            if nl.startswith("www."):
                nl = nl[4:]
            if (nl.endswith(":443") and parsed.scheme == "https") or \
               (nl.endswith(":80") and parsed.scheme == "http"):
                nl = nl.rsplit(":", 1)[0]
            return nl
        return _normalize_netloc(url) == _normalize_netloc(base_domain)
    except Exception:
        return False


def _fetch(url: str, timeout: float) -> FetchResult:
    """GET with one retry on 5xx/timeout. Never raises.

    Returns the response when the server answered with <500, otherwise a
    FetchResult carrying the reason so the caller can explain the failure.
    """
    last = FetchResult(code="unknown", detail="нет ответа")
    for attempt in range(2):
        try:
            r = httpx.get(
                url, timeout=timeout, follow_redirects=True, headers=_DEFAULT_HEADERS
            )
            if r.status_code < 500:
                return FetchResult(response=r)
            code, detail = _status_reason(r.status_code)
            last = FetchResult(code=code, detail=detail)
            logger.warning(f"GET {url} -> {r.status_code} (attempt {attempt + 1}/2)")
        except httpx.TimeoutException as e:
            last = FetchResult(code="timeout", detail=_REASONS["timeout"])
            logger.warning(f"GET {url} failed (attempt {attempt + 1}/2): {e}")
        except httpx.ConnectError as e:
            code, detail = _classify_transport_error(e)
            last = FetchResult(code=code, detail=detail)
            logger.warning(f"GET {url} failed (attempt {attempt + 1}/2): {e}")
        except Exception as e:
            last = FetchResult(code="unknown", detail=f"{type(e).__name__}")
            logger.warning(f"GET {url} unexpected error: {e}")
            return last
    return last


def _extract_links_from_homepage(html: str, base_url: str, limit: int) -> list[str]:
    """Parse homepage HTML, extract same-domain links."""
    try:
        tree = HTMLParser(html)
    except Exception as e:
        logger.warning(f"Failed to parse homepage HTML: {e}")
        return []
    seen: dict[str, None] = {}
    for a in tree.css("a[href]"):
        href = a.attributes.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        # Normalize: strip fragment
        absolute = absolute.split("#", 1)[0]
        if _is_same_domain(absolute, base_url) and absolute:
            seen[absolute] = None
            if len(seen) >= limit:
                break
    return list(seen)


def _crawl_sitemap_recursive(
    sitemap_url: str,
    timeout: float,
    depth: int,
    seen: dict[str, None],
    outcome: dict | None = None,
) -> None:
    """Recursively fetch a sitemap (or sitemapindex) and add URLs to `seen`.

    `outcome` collects the failure codes and HTTP statuses seen along the way so
    the orchestrator can explain *why* nothing was found.
    """
    if depth > _MAX_RECURSION_DEPTH or sitemap_url in seen:
        return
    seen[sitemap_url] = None  # mark as visited to prevent loops
    fetched = _fetch(sitemap_url, timeout)
    if outcome is not None:
        if fetched.response is not None:
            outcome["statuses"].append(fetched.response.status_code)
        if fetched.code:
            outcome["codes"].append(fetched.code)
    if fetched.response is None or fetched.response.status_code != 200:
        return
    for url in _parse_sitemap_xml(fetched.response.content):
        if _is_same_domain(url, sitemap_url):
            # Recurse first (if sitemapindex child) so the recursion check
            # doesn't bail on the new URL being added to `seen`.
            if url.endswith(".xml") or "/sitemap" in url:
                _crawl_sitemap_recursive(url, timeout, depth + 1, seen, outcome)
            seen[url] = None


def discover_urls_detailed(
    domain_url: str,
    *,
    timeout: float = 15.0,
    max_urls: int = 500,
) -> DiscoveryResult:
    """Discover URLs on a site and explain *why* it failed when it does.

    Never raises. Strategy (1-to-1 with firecrawl /v2/map):
    1. Try GET /robots.txt -> extract Sitemap: URLs (if any)
    2. Also try /sitemap.xml and /sitemap_index.xml
    3. For each sitemap URL: recurse (handles sitemapindex), parse <loc>
    4. If no URLs found: fetch homepage, extract <a href> links (max 50)
    5. Filter to same-domain, dedupe, cap at max_urls (sets `truncated`)
    """
    parsed = urlparse(domain_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    seen: dict[str, None] = {}
    outcome: dict = {"codes": [], "statuses": []}

    # 1. robots.txt -> additional sitemap candidates
    sitemap_candidates: list[str] = []
    robots = _fetch(f"{base}/robots.txt", timeout)
    if robots.response is not None:
        outcome["statuses"].append(robots.response.status_code)
        if robots.response.status_code == 200:
            sitemap_candidates.extend(_extract_robots_sitemaps(robots.response.text))
        elif robots.code:
            outcome["codes"].append(robots.code)
    elif robots.code:
        outcome["codes"].append(robots.code)

    # 2. Default sitemap paths (only if robots.txt didn't provide any)
    if not sitemap_candidates:
        for path in ("/sitemap.xml", "/sitemap_index.xml"):
            sitemap_candidates.append(f"{base}{path}")

    # Dedupe candidate URLs
    sitemap_candidates = list(dict.fromkeys(sitemap_candidates))

    # 3. Crawl each sitemap recursively
    for sm in sitemap_candidates:
        _crawl_sitemap_recursive(sm, timeout, depth=0, seen=seen, outcome=outcome)
        if len(seen) > max_urls:
            break

    # Filter sitemaps themselves (entries ending in .xml) — keep only page URLs
    page_urls: list[str] = []
    for url in seen:
        if not (url.endswith(".xml") or url.endswith(".xml.gz")):
            page_urls.append(url)

    code = "ok"
    detail = ""
    status: int | None = None

    # 4. Link discovery fallback if no pages found
    if not page_urls:
        logger.info(f"No sitemap URLs for {domain_url}, trying homepage link discovery")
        home = _fetch(f"{base}/", timeout)
        if home.response is not None:
            status = home.response.status_code
            # 202 is what JS challenge interstitials answer with (measured on
            # real competitors), so any 2xx body is inspected for links.
            if 200 <= status < 300:
                body = home.response.text
                links = _extract_links_from_homepage(
                    body, base, _LINK_FALLBACK_LIMIT
                )
                if links:
                    page_urls = links
                elif len(body.encode("utf-8", errors="ignore")) < _CHALLENGE_MAX_BYTES:
                    code, detail = "challenge", _REASONS["challenge"]
                else:
                    code, detail = "empty_page", _REASONS["empty_page"]
            else:
                code, detail = _status_reason(status)
        else:
            code, detail = home.code or "unknown", home.detail or _REASONS["refused"]
        if not page_urls and code != "ok":
            # A unanimous block status from every sitemap source is a stronger
            # signal than a flaky homepage request: report being blocked.
            blocked = _unanimous_block_status(outcome)
            if blocked is not None:
                code, detail = blocked
    else:
        status = None

    # 5. Final filter: same-domain, cap, return
    page_urls = [u for u in page_urls if _is_same_domain(u, base)]
    page_urls = list(dict.fromkeys(page_urls))  # dedupe preserve order

    truncated = False
    if len(page_urls) > max_urls:
        truncated = True
        logger.warning(
            f"{domain_url}: discovered {len(page_urls)} URLs, truncating to {max_urls}"
        )
        page_urls = page_urls[:max_urls]

    ok = bool(page_urls) and code == "ok"
    logger.info(f"Discovered {len(page_urls)} URLs for {domain_url}")
    return DiscoveryResult(
        urls=page_urls,
        ok=ok,
        code=code,
        http_status=status,
        truncated=truncated,
        detail=detail,
    )


def _unanimous_block_status(outcome: dict) -> tuple[str, str] | None:
    """Return (code, detail) when every sitemap source said the same thing.

    Only 403 and 429 qualify: they describe *us* being blocked rather than the
    site being down, which is the distinction the report needs to make.
    """
    statuses = [s for s in outcome["statuses"] if s]
    if not statuses or len(set(statuses)) != 1:
        return None
    status = statuses[0]
    if status in (403, 429):
        return _status_reason(status)
    return None


def discover_urls(
    domain_url: str,
    *,
    timeout: float = 15.0,
    max_urls: int = 500,
) -> list[str]:
    """Discover URLs on a site via sitemap (and link fallback).

    Backwards-compatible wrapper around `discover_urls_detailed`; returns
    deduplicated, same-domain URLs only, [] on any failure.
    """
    return discover_urls_detailed(
        domain_url, timeout=timeout, max_urls=max_urls
    ).urls
