"""Inline sitemap + link discovery, replacing firecrawl /v2/map."""
import re
import xml.etree.ElementTree as ET
import httpx
from selectolax.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from loguru import logger
from typing import Final

_SITEMAP_NS: Final = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_SITEMAP_RE: Final = re.compile(
    r"^\s*Sitemap\s*:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE
)


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


def _fetch(url: str, timeout: float) -> httpx.Response | None:
    """GET with one retry on 5xx/timeout. Returns None on final failure."""
    for attempt in range(2):
        try:
            r = httpx.get(
                url, timeout=timeout, follow_redirects=True, headers=_DEFAULT_HEADERS
            )
            if r.status_code < 500:
                return r
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.warning(f"GET {url} failed (attempt {attempt + 1}/2): {e}")
        except Exception as e:
            logger.warning(f"GET {url} unexpected error: {e}")
            return None
    return None


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
    sitemap_url: str, timeout: float, depth: int, seen: dict[str, None]
) -> None:
    """Recursively fetch a sitemap (or sitemapindex) and add URLs to `seen`."""
    if depth > _MAX_RECURSION_DEPTH or sitemap_url in seen:
        return
    seen[sitemap_url] = None  # mark as visited to prevent loops
    resp = _fetch(sitemap_url, timeout)
    if resp is None or resp.status_code != 200:
        return
    for url in _parse_sitemap_xml(resp.content):
        if _is_same_domain(url, sitemap_url):
            # Recurse first (if sitemapindex child) so the recursion check
            # doesn't bail on the new URL being added to `seen`.
            if url.endswith(".xml") or "/sitemap" in url:
                _crawl_sitemap_recursive(url, timeout, depth + 1, seen)
            seen[url] = None


def discover_urls(
    domain_url: str,
    *,
    timeout: float = 15.0,
    max_urls: int = 500,
) -> list[str]:
    """Discover URLs on a site via sitemap (and link fallback).

    Returns deduplicated, same-domain URLs only. Never raises — returns
    [] on any error (e.g. domain unreachable, all sitemaps 5xx).

    Strategy (1-to-1 with firecrawl /v2/map):
    1. Try GET /robots.txt → extract Sitemap: URLs (if any)
    2. Also try /sitemap.xml and /sitemap_index.xml
    3. For each sitemap URL: recurse (handles sitemapindex), parse <loc>
    4. If no URLs found: fetch homepage, extract <a href> links (max 50)
    5. Filter to same-domain, dedupe, cap at max_urls
    """
    parsed = urlparse(domain_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    seen: dict[str, None] = {}

    # 1. robots.txt → additional sitemap candidates
    sitemap_candidates: list[str] = []
    robots_resp = _fetch(f"{base}/robots.txt", timeout)
    if robots_resp is not None and robots_resp.status_code == 200:
        sitemap_candidates.extend(_extract_robots_sitemaps(robots_resp.text))

    # 2. Default sitemap paths (only if robots.txt didn't provide any)
    if not sitemap_candidates:
        for path in ("/sitemap.xml", "/sitemap_index.xml"):
            sitemap_candidates.append(f"{base}{path}")

    # Dedupe candidate URLs
    sitemap_candidates = list(dict.fromkeys(sitemap_candidates))

    # 3. Crawl each sitemap recursively
    for sm in sitemap_candidates:
        _crawl_sitemap_recursive(sm, timeout, depth=0, seen=seen)
        if len(seen) > max_urls:
            break

    # Filter sitemaps themselves (entries ending in .xml) — keep only page URLs
    page_urls: list[str] = []
    for url in seen:
        if not (url.endswith(".xml") or url.endswith(".xml.gz")):
            page_urls.append(url)

    # 4. Link discovery fallback if no pages found
    if not page_urls:
        logger.info(f"No sitemap URLs for {domain_url}, trying homepage link discovery")
        home_resp = _fetch(f"{base}/", timeout)
        if home_resp is not None and home_resp.status_code == 200:
            page_urls = _extract_links_from_homepage(
                home_resp.text, base, _LINK_FALLBACK_LIMIT
            )

    # 5. Final filter: same-domain, cap, return
    page_urls = [u for u in page_urls if _is_same_domain(u, base)]
    page_urls = list(dict.fromkeys(page_urls))  # dedupe preserve order

    if len(page_urls) > max_urls:
        logger.warning(
            f"{domain_url}: discovered {len(page_urls)} URLs, truncating to {max_urls}"
        )
        page_urls = page_urls[:max_urls]

    logger.info(f"Discovered {len(page_urls)} URLs for {domain_url}")
    return page_urls
