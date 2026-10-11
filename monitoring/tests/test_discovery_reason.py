"""Failure taxonomy for discovery: every empty result must explain itself."""
from unittest.mock import MagicMock, patch

import httpx

from src.services.discovery.sitemap import (
    DiscoveryResult,
    discover_urls,
    discover_urls_detailed,
)


def _resp(status_code: int, text: str = "", url: str = "https://x.test/"):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = text.encode("utf-8")
    resp.url = httpx.URL(url)
    return resp


def _run(responses=None, error=None, domain="https://example.com"):
    def fake_get(url, **kwargs):
        if error is not None:
            raise error
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        return discover_urls_detailed(domain)


# --- transport-level failures -----------------------------------------


def test_dns_failure_is_reported_as_dns():
    err = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    result = _run(error=err)
    assert result.ok is False
    assert result.code == "dns"
    assert result.detail == "домен не резолвится"
    assert result.urls == []


def test_connection_refused_is_reported_as_refused():
    err = httpx.ConnectError("All connection attempts failed: Connection refused")
    result = _run(error=err)
    assert result.code == "refused"
    assert result.detail == "сервер не отвечает"


def test_tls_failure_is_reported_as_tls():
    err = httpx.ConnectError("certificate verify failed: self signed certificate")
    result = _run(error=err)
    assert result.code == "tls"
    assert result.detail == "ошибка TLS/сертификата"


def test_timeout_is_reported_as_timeout():
    result = _run(error=httpx.ConnectTimeout("timed out"))
    assert result.code == "timeout"
    assert result.detail == "таймаут ответа"


def test_unanimous_block_beats_a_flaky_homepage_request():
    """robots/sitemap all 403 but the homepage times out -> report the block.

    Reporting "таймаут" would hide the fact that the site is deliberately
    refusing this bot, which is the actionable part.
    """
    responses = {
        "https://example.com/robots.txt": _resp(403),
        "https://example.com/sitemap.xml": _resp(403),
        "https://example.com/sitemap_index.xml": _resp(403),
    }

    def fake_get(url, **kwargs):
        if url.endswith("/"):
            raise httpx.ConnectTimeout("timed out")
        return responses[url]

    with patch("src.services.discovery.sitemap.httpx.get", side_effect=fake_get):
        result = discover_urls_detailed("https://example.com")
    assert result.code == "http_403"
    assert result.detail == "403 — блокируют бота"


# --- HTTP status failures ---------------------------------------------


def test_403_on_everything_means_bot_block():
    responses = {
        "https://example.com/robots.txt": _resp(403),
        "https://example.com/sitemap.xml": _resp(403),
        "https://example.com/sitemap_index.xml": _resp(403),
        "https://example.com/": _resp(403),
    }
    result = _run(responses)
    assert result.ok is False
    assert result.code == "http_403"
    assert result.detail == "403 — блокируют бота"
    assert result.http_status == 403


def test_429_is_reported_as_rate_limited():
    responses = {
        "https://example.com/robots.txt": _resp(429),
        "https://example.com/sitemap.xml": _resp(429),
        "https://example.com/sitemap_index.xml": _resp(429),
        "https://example.com/": _resp(429),
    }
    result = _run(responses)
    assert result.code == "http_429"
    assert result.detail == "429 — нас ограничивают"


def test_5xx_everywhere_is_reported_as_server_error():
    responses = {
        "https://example.com/robots.txt": _resp(500),
        "https://example.com/sitemap.xml": _resp(503),
        "https://example.com/sitemap_index.xml": _resp(500),
        "https://example.com/": _resp(500),
    }
    result = _run(responses)
    assert result.code == "http_5xx"
    assert result.detail == "5xx на сайте"


def test_no_sitemap_and_no_links_is_empty_page():
    responses = {
        "https://example.com/robots.txt": _resp(404),
        "https://example.com/sitemap.xml": _resp(404),
        "https://example.com/sitemap_index.xml": _resp(404),
        "https://example.com/": _resp(200, "<html><body>" + "<p>hi</p>" * 400 + "</body></html>"),
    }
    result = _run(responses)
    assert result.ok is False
    assert result.code == "empty_page"
    assert result.detail == "главная без ссылок (JS-сайт)"


def test_tiny_homepage_is_a_js_challenge():
    responses = {
        "https://example.com/robots.txt": _resp(404),
        "https://example.com/sitemap.xml": _resp(404),
        "https://example.com/sitemap_index.xml": _resp(404),
        "https://example.com/": _resp(202, "<html>Please enable JavaScript</html>"),
    }
    result = _run(responses)
    assert result.code == "challenge"
    assert result.detail == "JS-защита (заглушка)"


def test_404_on_homepage_means_the_site_is_gone():
    """robots/sitemap 404 + homepage 404 => the whole path is dead.

    This is more precise than the "no sitemap" reason: for a competitor list
    it means the domain itself stopped serving pages.
    """
    responses = {
        "https://example.com/robots.txt": _resp(404),
        "https://example.com/sitemap.xml": _resp(404),
        "https://example.com/sitemap_index.xml": _resp(404),
        "https://example.com/": _resp(404),
    }
    result = _run(responses)
    assert result.code == "http_404"
    assert result.detail == "404 — не найдено"
    assert result.http_status == 404


# --- success paths -----------------------------------------------------


def test_sitemap_success_has_no_failure_detail():
    sitemap_body = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://example.com/a</loc></url>
    </urlset>"""
    responses = {
        "https://example.com/robots.txt": _resp(200, "User-agent: *"),
        "https://example.com/sitemap.xml": _resp(200, sitemap_body),
    }
    result = _run(responses)
    assert result.ok is True
    assert result.code == "ok"
    assert result.detail == ""
    assert result.urls == ["https://example.com/a"]


def test_truncation_is_flagged_but_not_a_failure():
    urls = "\n".join(
        f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(30)
    )
    body = (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + urls
        + "</urlset>"
    )
    responses = {
        "https://example.com/robots.txt": _resp(200, "User-agent: *"),
        "https://example.com/sitemap.xml": _resp(200, body),
    }
    with patch("src.services.discovery.sitemap.httpx.get", side_effect=lambda u, **k: responses[u]):
        result = discover_urls_detailed("https://example.com", max_urls=10)
    assert result.truncated is True
    assert result.ok is True
    assert len(result.urls) == 10


# --- backwards compatibility ------------------------------------------


def test_discover_urls_still_returns_a_plain_list_of_strings():
    """The 6 existing tests rely on this contract; keep it byte-identical."""
    body = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://example.com/a</loc></url>
    <url><loc>https://example.com/b</loc></url>
    </urlset>"""
    responses = {
        "https://example.com/robots.txt": _resp(200, "User-agent: *"),
        "https://example.com/sitemap.xml": _resp(200, body),
        "https://example.com/sitemap_index.xml": _resp(404),
    }
    with patch("src.services.discovery.sitemap.httpx.get", side_effect=lambda u, **k: responses[u]):
        urls = discover_urls("https://example.com")
    assert isinstance(urls, list)
    assert all(isinstance(u, str) for u in urls)
    assert sorted(urls) == ["https://example.com/a", "https://example.com/b"]


def test_discovery_result_dataclass_defaults():
    result = DiscoveryResult()
    assert result.urls == []
    assert result.ok is False
    assert result.truncated is False