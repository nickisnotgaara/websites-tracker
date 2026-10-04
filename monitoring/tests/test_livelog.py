"""Tests for the live Telegram log (src.services.bot.livelog) and the
per-shop failure isolation it depends on (src.services.monitor).

Async code is driven with asyncio.run() inside sync tests so the suite needs
no extra dependency (no pytest-asyncio).
"""
import asyncio
import sys
import threading
import types

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from loguru import logger

from src.services.bot.livelog import (
    TELEGRAM_TEXT_LIMIT,
    LiveLogMessage,
    LogBuffer,
    RunProgress,
    _make_filter,
    install_sink,
    render,
)


# --- helpers -------------------------------------------------------------


class _FakeClock:
    """Deterministic clock: sleeps advance time instead of blocking."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        # Yield to the loop so a zero-duration interval cannot become a
        # tight non-yielding spin inside LiveLogMessage._loop.
        await asyncio.sleep(0)


class _Sent:
    def __init__(self, message_id):
        self.message_id = message_id


class _FakeBot:
    """Mirrors the real aiogram signatures *and* enforces their types.

    aiogram's two methods are asymmetric and both are positional-first:
        Bot.send_message(chat_id, text, ...)
        Bot.edit_message_text(text, ..., chat_id=, message_id=)
    They build pydantic models, so an int where a str belongs raises
    ValidationError before any HTTP call. The type checks below reproduce that
    contract so a wrong argument order fails here instead of in production.
    """

    def __init__(self, clock=None, error=None, fail_times=1):
        self.calls = []
        self.times = []
        self.clock = clock or (lambda: 0.0)
        self._error = error
        self._fail_times = fail_times

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if not isinstance(chat_id, int):
            raise TypeError("send_message: chat_id must be int")
        if not isinstance(text, str):
            raise TypeError("send_message: text must be str")
        self.calls.append(
            {"kind": "send", "chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        return _Sent(message_id=42)

    async def edit_message_text(
        self, text, chat_id=None, message_id=None, parse_mode=None, **kwargs
    ):
        if not isinstance(text, str):
            raise TypeError("edit_message_text: text must be str")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            raise TypeError("edit_message_text: chat_id/message_id must be int")
        self.calls.append(
            {
                "kind": "edit",
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        self.times.append(self.clock())
        if self._error is not None and self._fail_times > 0:
            self._fail_times -= 1
            raise self._error
        return _Sent(message_id=message_id)


def _make_live(bot, *, min_gap=0.0):
    clock = _FakeClock()
    bot.clock = clock
    buffer = LogBuffer(maxlen=500)
    progress = RunProgress()
    live = LiveLogMessage(
        bot,
        111,
        buffer,
        progress,
        interval=0.0,
        min_gap=min_gap,
        clock=clock,
        sleep=clock.sleep,
    )
    live.message_id = 42  # skip start(); the fake bot would send fine too
    return live, buffer, progress, clock


def _load_monitor():
    """Import the monitor service with Google Sheets always stubbed.

    Two reasons: src.services.gsheets authenticates at import time (it needs a
    real credentials.json), and the unit tests must never read or write the
    real competitor spreadsheet.
    """
    if "src.services.gsheets" not in sys.modules:
        stub = types.ModuleType("src.services.gsheets")

        class _StubGSheetsService:
            def get_competitors(self):
                return list(_STUB["competitors"])

            def log_new_collection(self, *args, **kwargs):
                return None

        stub.gsheets_service = _StubGSheetsService()
        sys.modules["src.services.gsheets"] = stub

    import src.services.monitor as monitor_module

    return monitor_module


_STUB = {"competitors": []}


# --- render: budget ------------------------------------------------------


def test_render_stays_within_4096():
    buffer = LogBuffer(maxlen=6000)
    for i in range(5000):
        buffer.append(f"04:12:{i % 60:02d} | INFO    | Discovered {i} URLs for shop-{i}")
    progress = RunProgress()
    progress.set_total(30)
    for i in range(30):
        progress.begin(f"https://shop-{i}.example.com")
        progress.finish(f"https://shop-{i}.example.com", "ok", new_urls=i)

    text = render(progress, buffer)
    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert len(text) > 1000  # still useful, not an empty stub


def test_render_keeps_newest_lines():
    buffer = LogBuffer(maxlen=500)
    filler = "x" * 120
    for i in range(300):
        buffer.append(f"line-{i:03d} {filler}")
    text = render(RunProgress(), buffer)
    assert "line-299" in text
    assert "line-000" not in text
    assert "строк выше" in text  # dropped-lines marker


# --- render: escaping ----------------------------------------------------


def test_render_escapes_html():
    buffer = LogBuffer(maxlen=10)
    buffer.append('12:00:01 | ERROR   | <b>bad</b> markup & "quotes"')
    text = render(RunProgress(), buffer)
    assert "&lt;b&gt;bad&lt;/b&gt;" in text
    assert "&amp;" in text
    # the only real tags are ours: the title and the "Логи:" label
    assert text.count("<b>") == 2
    assert text.count("</b>") == 2


# --- render: header / shops ---------------------------------------------


def test_render_header_shows_progress():
    progress = RunProgress()
    progress.set_total(9)
    progress.begin("https://modanost.example.com")
    text = render(progress, LogBuffer(maxlen=10))
    assert "1/9" in text
    assert "modanost.example.com" in text
    assert "⏳" in text


def test_render_marks_failed_shop_with_reason():
    progress = RunProgress()
    progress.set_total(2)
    progress.begin("https://shop-vika.example.com")
    progress.finish(
        "https://shop-vika.example.com", "failed", 0, "0 URLs discovered after 2 attempts"
    )
    text = render(progress, LogBuffer(maxlen=10))
    assert "❌" in text
    assert "0 URLs discovered after 2 attempts" in text


def test_render_compacts_long_shop_list():
    progress = RunProgress()
    progress.set_total(30)
    for i in range(30):
        url = f"https://shop-{i:02d}.example.com"
        progress.begin(url)
        progress.finish(url, "ok", new_urls=1)
    text = render(progress, LogBuffer(maxlen=10))
    # 12 live lines total: 11 shops + 1 marker
    assert "ещё 19 магазинов" in text
    assert "shop-29.example.com" in text  # newest kept
    assert "shop-00.example.com" not in text


def test_header_counts_baseline_skipped_sites():
    """A first run must not read as "everything failed": the 262 baseline
    sites were indexed fine, they just have nothing new to report."""
    progress = RunProgress()
    progress.set_total(3)
    progress.begin("https://a.example")
    progress.finish("https://a.example", "skipped", 0)
    progress.begin("https://b.example")
    progress.finish("https://b.example", "ok", 4)
    progress.begin("https://c.example")
    progress.finish("https://c.example", "failed", 0, "0 URLs discovered after 2 attempts")

    text = render(progress, LogBuffer(maxlen=10))
    assert "✅ 1 · ❌ 1 · ⏭️ 1" in text
    assert "🆕 4" in text


def test_failed_shops_are_listed_before_ok_ones():
    """With 300+ competitors the failures are the oldest entries; a plain
    "last N" window would hide all of them."""
    progress = RunProgress()
    progress.set_total(30)
    for i in range(30):
        url = f"https://shop-{i:02d}.example.com"
        progress.begin(url)
        if i < 5:  # failures came first, like a real run
            progress.finish(url, "failed", 0, "0 URLs discovered after 2 attempts")
        else:
            progress.finish(url, "skipped", 0)

    text = render(progress, LogBuffer(maxlen=10))
    for i in range(5):
        assert f"shop-{i:02d}.example.com" in text
    assert "shop-29.example.com" in text
    assert "shop-20.example.com" not in text  # healthy tail dropped first
    assert "ошибки — вверху" in text


def test_finished_report_lists_every_failure():
    progress = RunProgress()
    progress.set_total(44 + 262)
    for i in range(44):
        url = f"https://broken-{i:02d}.example.com"
        progress.begin(url)
        progress.finish(url, "failed", 0, "0 URLs discovered after 2 attempts")
    for i in range(262):
        url = f"https://ok-{i:03d}.example.com"
        progress.begin(url)
        progress.finish(url, "skipped", 0)
    progress.finish_run()

    buffer = LogBuffer(maxlen=600)
    for i in range(600):
        buffer.append(f"12:00:00 | INFO    | noise line {i}")

    text = render(progress, buffer)
    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert "Парсинг завершён" in text
    assert "⏭️ 262" in text
    for i in range(44):  # every failure is visible in the finished report
        assert f"broken-{i:02d}.example.com" in text
    assert "ещё" in text and "строк выше" in text  # log tail yielded to failures


def test_render_sheds_log_tail_before_shop_lines():
    progress = RunProgress()
    progress.set_total(40)
    for i in range(40):
        url = f"https://shop-{i:02d}.example.com"
        progress.begin(url)
        progress.finish(url, "failed" if i < 20 else "skipped", 0)
    progress.finish_run()

    buffer = LogBuffer(maxlen=2000)
    for i in range(2000):
        buffer.append(f"12:00:00 | INFO    | {'x' * 150} {i}")

    text = render(progress, buffer)
    assert len(text) <= TELEGRAM_TEXT_LIMIT
    for i in range(20):  # failures survive the budget squeeze
        assert f"shop-{i:02d}.example.com" in text


# --- flush: dedupe and throttling ---------------------------------------


def test_flush_skips_identical_render():
    bot = _FakeBot()
    live, buffer, progress, _ = _make_live(bot)
    buffer.append("first line")

    asyncio.run(live._flush())
    asyncio.run(live._flush())

    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert len(edits) == 1  # second flush was a no-op -> never sent


def test_edit_uses_aiogram_argument_order():
    """Regression: aiogram takes edit_message_text(text, chat_id=, message_id=)
    but send_message(chat_id, text). Swapping them raises ValidationError in
    production while a permissive fake would happily accept it."""
    bot = _FakeBot()
    # Real asyncio.sleep + long interval: the refresh task never actually fires.
    live = LiveLogMessage(bot, 111, LogBuffer(maxlen=100), RunProgress(), min_gap=0.0)

    async def scenario():
        await live.start()
        live.buffer.append("line 1")
        await live._flush()
        await live.stop()

    asyncio.run(scenario())

    sent = [c for c in bot.calls if c["kind"] == "send"]
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 111
    assert sent[0]["text"] == "🩺 Запускаю парсинг…"

    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert len(edits) == 1
    assert edits[0]["chat_id"] == 111
    assert edits[0]["message_id"] == 42
    assert isinstance(edits[0]["text"], str)
    assert "Парсинг конкурентов" in edits[0]["text"]


def test_flush_respects_min_gap():
    bot = _FakeBot()
    live, buffer, progress, clock = _make_live(bot, min_gap=2.0)

    async def scenario():
        buffer.append("line 1")
        await live._flush()
        buffer.append("line 2")
        await live._flush()
        buffer.append("line 3")
        await live._flush()

    asyncio.run(scenario())

    assert bot.times == [0.0, 2.0, 4.0]
    assert clock.sleeps == [2.0, 2.0]


# --- flush: Telegram error handling -------------------------------------


def test_flush_honors_retry_after():
    error = TelegramRetryAfter(
        method=None,
        message="Too Many Requests: retry after 3",
        retry_after=3,
    )
    bot = _FakeBot(error=error, fail_times=1)
    live, buffer, progress, clock = _make_live(bot)
    buffer.append("line 1")

    asyncio.run(live._flush())

    assert 3.5 in clock.sleeps  # exact retry_after + jitter
    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert len(edits) == 2  # failed attempt + successful retry
    assert live._penalty == 2  # rest of the run is throttled harder


def test_flush_falls_back_to_plain_text():
    error = TelegramBadRequest(
        method=None, message="Bad Request: can't parse entities: unsupported start tag"
    )
    bot = _FakeBot(error=error, fail_times=1)
    live, buffer, progress, _ = _make_live(bot)
    buffer.append("12:00:00 | ERROR   | <b>oops</b> & broken")

    asyncio.run(live._flush())

    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert len(edits) == 2
    assert edits[1]["parse_mode"] is None
    assert "&lt;b&gt;" not in edits[1]["text"]
    assert "<b>oops</b> & broken" in edits[1]["text"]


def test_flush_stops_when_uneditable():
    error = TelegramBadRequest(
        method=None, message="Bad Request: message can't be edited"
    )
    bot = _FakeBot(error=error, fail_times=99)
    live, buffer, progress, _ = _make_live(bot)

    buffer.append("line 1")
    asyncio.run(live._flush())  # must not raise
    buffer.append("line 2")
    asyncio.run(live._flush())

    assert live._broken is True
    assert len([c for c in bot.calls if c["kind"] == "edit"]) == 1


# --- buffer -------------------------------------------------------------


def test_buffer_is_bounded_and_thread_safe():
    buffer = LogBuffer(maxlen=1000)

    def worker(n):
        for i in range(2500):
            buffer.append(f"{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(buffer) == 1000
    assert len(buffer.tail()) == 1000
    assert len(buffer.tail(10)) == 10
    buffer.clear()
    assert len(buffer) == 0


# --- loguru sink --------------------------------------------------------


def test_sink_filter_drops_internal_and_self_records():
    predicate = _make_filter(("aiogram", "src.services.bot.livelog"))
    assert predicate({"name": "src.services.monitor"}) is True
    assert predicate({"name": "aiogram"}) is False
    assert predicate({"name": "aiogram.client.session.aiohttp"}) is False
    assert predicate({"name": "src.services.bot.livelog"}) is False
    assert predicate({"name": "src.services.bot.livelog.LiveLogMessage"}) is False


def test_install_sink_writes_formatted_lines():
    buffer = LogBuffer(maxlen=50)
    handler = install_sink(buffer, level="INFO", enabled=True)
    assert handler is not None
    try:
        logger.info("Discovered 12 URLs for shop")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed thing")
    finally:
        logger.remove(handler)

    lines = buffer.tail()
    assert len(lines) == 2
    assert lines[0].endswith("INFO    | Discovered 12 URLs for shop")
    assert "↳ ValueError: boom" in lines[1]
    # DEBUG is filtered out at the requested level
    logger.debug("not mirrored")
    assert len(buffer) == 2


def test_install_sink_disabled():
    assert install_sink(LogBuffer(maxlen=10), enabled=False) is None


# --- monitor integration ------------------------------------------------


def test_run_check_cycle_continues_after_site_failure(monkeypatch):
    monitor_module = _load_monitor()
    _STUB["competitors"] = [
        "https://a.example.com",
        "https://b.example.com",
        "https://c.example.com",
    ]

    async def fake_check(domain):
        if "b.example.com" in domain:
            raise RuntimeError("boom")
        return {"status": "ok", "new_urls": 2, "error": None}

    monkeypatch.setattr(monitor_module.monitor_service, "check_competitor", fake_check)

    progress = RunProgress()
    asyncio.run(monitor_module.monitor_service.run_check_cycle(progress))

    assert progress.total == 3
    assert progress.ok_count == 2
    assert progress.failed_count == 1
    assert progress.total_new == 4
    failed = [s for s in progress.shops if s.state == "failed"]
    assert "RuntimeError: boom" in failed[0].error


def test_check_competitor_reports_zero_urls_as_failed(monkeypatch):
    monitor_module = _load_monitor()
    monkeypatch.setattr(
        monitor_module.discovery, "discover_urls", lambda url, **kwargs: []
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = asyncio.run(
        monitor_module.monitor_service.check_competitor("https://empty.example.com")
    )

    assert result["status"] == "failed"
    assert "0 URLs" in result["error"]
    assert result["new_urls"] == 0
