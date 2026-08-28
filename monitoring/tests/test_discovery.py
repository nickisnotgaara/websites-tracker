"""Tests for src.services.discovery (sitemap + scrape)."""
from src.services.discovery.sitemap import _extract_robots_sitemaps, _parse_sitemap_xml


# --- _extract_robots_sitemaps ---

def test_extract_robots_sitemaps_no_sitemap_line():
    text = "User-agent: *\nDisallow: /admin\n"
    assert _extract_robots_sitemaps(text) == []


def test_extract_robots_sitemaps_single():
    text = "User-agent: *\nDisallow: /\nSitemap: https://example.com/sitemap.xml\n"
    assert _extract_robots_sitemaps(text) == ["https://example.com/sitemap.xml"]


def test_extract_robots_sitemaps_multiple():
    text = (
        "Sitemap: https://a.com/sitemap.xml\n"
        "Sitemap: https://b.com/sitemap.xml\n"
        "User-agent: *\n"
    )
    assert _extract_robots_sitemaps(text) == [
        "https://a.com/sitemap.xml",
        "https://b.com/sitemap.xml",
    ]


def test_extract_robots_sitemaps_case_insensitive():
    text = "SITEMAP: https://example.com/sitemap.xml\n"
    assert _extract_robots_sitemaps(text) == ["https://example.com/sitemap.xml"]


def test_extract_robots_sitemaps_strips_whitespace():
    text = "Sitemap:    https://example.com/sitemap.xml   \n"
    assert _extract_robots_sitemaps(text) == ["https://example.com/sitemap.xml"]


def test_extract_robots_sitemaps_ignores_other_directives():
    text = (
        "User-agent: Googlebot\n"
        "Allow: /\n"
        "Sitemap: https://example.com/sitemap.xml\n"
        "User-agent: BadBot\n"
        "Disallow: /\n"
    )
    assert _extract_robots_sitemaps(text) == ["https://example.com/sitemap.xml"]


# --- _parse_sitemap_xml (urlset) ---

URLSET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
  <url><loc>https://example.com/c</loc></url>
</urlset>"""


def test_parse_sitemap_xml_urlset():
    assert _parse_sitemap_xml(URLSET_XML) == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]


def test_parse_sitemap_xml_urlset_empty():
    xml = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>"""
    assert _parse_sitemap_xml(xml) == []


# --- _parse_sitemap_xml (sitemapindex) ---

SITEMAP_INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/s1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/s2.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_sitemap_xml_index():
    assert _parse_sitemap_xml(SITEMAP_INDEX_XML) == [
        "https://example.com/s1.xml",
        "https://example.com/s2.xml",
    ]


def test_parse_sitemap_xml_no_recursive_expansion():
    """Helper returns the sitemap URLs themselves, not the nested pages.
    Recursive expansion is the orchestrator's job."""
    assert _parse_sitemap_xml(SITEMAP_INDEX_XML) == [
        "https://example.com/s1.xml",
        "https://example.com/s2.xml",
    ]


# --- _parse_sitemap_xml (malformed input) ---

def test_parse_sitemap_xml_malformed_returns_empty():
    """Malformed XML should return empty list, not raise."""
    assert _parse_sitemap_xml(b"<not really xml") == []


def test_parse_sitemap_xml_empty_bytes():
    assert _parse_sitemap_xml(b"") == []
