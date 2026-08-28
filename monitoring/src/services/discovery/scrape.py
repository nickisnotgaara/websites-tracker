"""Inline article scrape (markdown), replacing firecrawl /v2/scrape."""
import httpx
import trafilatura
from loguru import logger

_DEFAULT_HEADERS = {"User-Agent": "competitor-monitor/1.0"}


def scrape_markdown(
    url: str,
    *,
    timeout: float = 30.0,
    max_chars: int = 50_000,
) -> str:
    """Fetch URL and return article content as markdown.

    Returns "" on any error (404, non-HTML, timeout, parse failure).
    Output truncated to ~max_chars at the last newline boundary.
    Never raises.
    """
    try:
        resp = httpx.get(
            url, timeout=timeout, follow_redirects=True, headers=_DEFAULT_HEADERS
        )
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning(f"Scrape timeout/network error for {url}: {e}")
        return ""
    except Exception as e:
        logger.warning(f"Scrape unexpected error for {url}: {e}")
        return ""

    if resp.status_code != 200:
        logger.warning(f"Scrape {resp.status_code} for {url}")
        return ""

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        logger.warning(f"Skipping non-HTML scrape for {url} (content-type: {content_type})")
        return ""

    try:
        markdown = trafilatura.extract(
            resp.text,
            output_format="markdown",
            include_links=False,
            include_images=False,
            include_tables=True,
            favor_precision=True,
        )
    except Exception as e:
        logger.warning(f"Trafilatura extraction failed for {url}: {e}")
        return ""

    if not markdown:
        return ""

    if len(markdown) > max_chars:
        # Truncate at last newline before limit
        cut = markdown.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars  # no good newline; hard cut
        markdown = markdown[:cut].rstrip()
        logger.debug(f"Truncated scrape of {url} to {cut} chars")

    return markdown
