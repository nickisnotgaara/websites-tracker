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


# --- discover_urls (orchestrator) ---

from unittest.mock import patch, MagicMock
import httpx

from src.services.discovery.sitemap import discover_urls


def _mock_response(status_code: int, text: str = "", url: str = "https://x.test/"):
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = text.encode("utf-8")
    resp.url = httpx.URL(url)
    return resp


def test_discover_urls_sitemap_only():
    """robots.txt has no Sitemap, /sitemap.xml returns urlset, return those URLs."""
    robots_body = "User-agent: *\nDisallow: /admin\n"
    sitemap_body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""

    responses = {
        "https://example.com/robots.txt": _mock_response(200, robots_body),
        "https://example.com/sitemap.xml": _mock_response(200, sitemap_body),
        "https://example.com/sitemap_index.xml": _mock_response(404),
    }

    def fake_get(url, **kwargs):
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    assert sorted(urls) == ["https://example.com/a", "https://example.com/b"]


def test_discover_urls_robots_sitemap_takes_precedence():
    """If robots.txt points to a sitemap, use that and skip default paths."""
    robots_body = "Sitemap: https://example.com/custom-sitemap.xml\n"
    sitemap_body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/x</loc></url>
</urlset>"""

    responses = {
        "https://example.com/robots.txt": _mock_response(200, robots_body),
        "https://example.com/custom-sitemap.xml": _mock_response(200, sitemap_body),
    }

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    assert urls == ["https://example.com/x"]
    # No call to default /sitemap.xml — robots Sitemap took precedence
    assert "https://example.com/sitemap.xml" not in calls


def test_discover_urls_no_sitemap_falls_back_to_link_discovery():
    """If all sitemap sources 404, try fetching homepage and extracting links."""
    robots_body = "User-agent: *\n"
    homepage_html = """
    <html><body>
      <a href="/about">About</a>
      <a href="/products">Products</a>
      <a href="https://example.com/contact">Contact</a>
      <a href="https://other.com/external">External</a>
      <a href="/">Home</a>
    </body></html>
    """

    responses = {
        "https://example.com/robots.txt": _mock_response(200, robots_body),
        "https://example.com/sitemap.xml": _mock_response(404),
        "https://example.com/sitemap_index.xml": _mock_response(404),
        "https://example.com/": _mock_response(200, homepage_html),
    }

    def fake_get(url, **kwargs):
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    # Same-domain only, deduplicated
    assert sorted(urls) == [
        "https://example.com/",
        "https://example.com/about",
        "https://example.com/contact",
        "https://example.com/products",
    ]
    # External URL filtered out
    assert not any("other.com" in u for u in urls)


def test_discover_urls_filters_cross_domain_from_sitemap():
    """Sitemaps can contain URLs from other domains — filter them out."""
    sitemap_body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://other-domain.com/x</loc></url>
</urlset>"""

    responses = {
        "https://example.com/robots.txt": _mock_response(404),
        "https://example.com/sitemap.xml": _mock_response(200, sitemap_body),
        "https://example.com/sitemap_index.xml": _mock_response(404),
    }

    def fake_get(url, **kwargs):
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    assert urls == ["https://example.com/a"]


def test_discover_urls_dedupes():
    """Same URL appearing twice in sitemap or across sources → once in output."""
    sitemap_body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/a</loc></url>
</urlset>"""

    responses = {
        "https://example.com/robots.txt": _mock_response(404),
        "https://example.com/sitemap.xml": _mock_response(200, sitemap_body),
        "https://example.com/sitemap_index.xml": _mock_response(404),
    }

    def fake_get(url, **kwargs):
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    assert urls == ["https://example.com/a"]


def test_discover_urls_all_sources_fail_returns_empty():
    """If all sources 5xx, return [] and don't raise."""
    responses = {
        "https://example.com/robots.txt": _mock_response(500),
        "https://example.com/sitemap.xml": _mock_response(503),
        "https://example.com/sitemap_index.xml": _mock_response(500),
    }

    def fake_get(url, **kwargs):
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    assert urls == []


def test_discover_urls_recurses_into_sitemap_index():
    """sitemap-index → nested sitemaps → pages."""
    index_body = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/nested1.xml</loc></sitemap>
</sitemapindex>"""
    nested_body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/from-nested</loc></url>
</urlset>"""

    responses = {
        "https://example.com/robots.txt": _mock_response(404),
        "https://example.com/sitemap.xml": _mock_response(200, index_body),
        "https://example.com/sitemap_index.xml": _mock_response(404),
        "https://example.com/nested1.xml": _mock_response(200, nested_body),
    }

    def fake_get(url, **kwargs):
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        urls = discover_urls("https://example.com")

    assert urls == ["https://example.com/from-nested"]


# --- scrape_markdown ---

from src.services.discovery.scrape import scrape_markdown


HTML_WITH_ARTICLE = """<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<nav><a href="/">Home</a></nav>
<article>
  <h1>Main Title</h1>
  <p>This is the first paragraph of the article with some content.</p>
  <p>This is the second paragraph with more details and explanations.</p>
</article>
<footer>Footer text here</footer>
</body>
</html>"""


def test_scrape_markdown_extracts_article():
    resp = _mock_response(200, HTML_WITH_ARTICLE)
    resp.headers = {"content-type": "text/html; charset=utf-8"}

    with patch("src.services.discovery.scrape.httpx.get", return_value=resp):
        result = scrape_markdown("https://example.com/article")

    assert isinstance(result, str)
    assert "Main Title" in result
    assert "first paragraph" in result
    # Navigation/footer usually stripped by trafilatura
    assert "Footer text" not in result


def test_scrape_markdown_404_returns_empty():
    resp = _mock_response(404)

    with patch("src.services.discovery.scrape.httpx.get", return_value=resp):
        result = scrape_markdown("https://example.com/missing")

    assert result == ""


def test_scrape_markdown_non_html_returns_empty():
    resp = _mock_response(200, "binary junk")
    resp.headers = {"content-type": "application/pdf"}

    with patch("src.services.discovery.scrape.httpx.get", return_value=resp):
        result = scrape_markdown("https://example.com/file.pdf")

    assert result == ""


def test_scrape_markdown_timeout_returns_empty():
    with patch(
        "src.services.discovery.scrape.httpx.get",
        side_effect=httpx.TimeoutException("timed out"),
    ):
        result = scrape_markdown("https://example.com/slow")

    assert result == ""


def test_scrape_markdown_truncates_at_max_chars():
    """Long content gets truncated to ~max_chars at the last newline."""
    long_text = "lorem ipsum dolor sit amet. " * 5000  # ~250KB
    html = f"<!DOCTYPE html><html><body><article><p>{long_text}</p></article></body></html>"
    resp = _mock_response(200, html)
    resp.headers = {"content-type": "text/html; charset=utf-8"}

    with patch("src.services.discovery.scrape.httpx.get", return_value=resp):
        result = scrape_markdown("https://example.com/long", max_chars=1000)

    assert len(result) <= 1000
    # Truncation at last newline → result should not end mid-word
    assert not result.endswith(" amet")  # was truncated
