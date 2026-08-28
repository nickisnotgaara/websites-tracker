"""Inline sitemap + link discovery, replacing firecrawl /v2/map."""
import re
import xml.etree.ElementTree as ET
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
