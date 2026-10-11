"""Inline discovery (sitemap + scrape), replacing firecrawl."""
from .scrape import scrape_markdown
from .sitemap import (
    DiscoveryResult,
    FetchResult,
    discover_urls,
    discover_urls_detailed,
)

__all__ = [
    "discover_urls",
    "discover_urls_detailed",
    "scrape_markdown",
    "DiscoveryResult",
    "FetchResult",
]