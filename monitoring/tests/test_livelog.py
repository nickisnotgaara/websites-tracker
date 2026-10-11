"""Tests for the live Telegram log (src.services.bot.livelog) and the
per-shop failure isolation it depends on (src.services.monitor).

Async code is driven with asyncio.run() inside sync tests so the suite needs
no extra dependency (no pytest-asyncio).
"""
import asyncio
import sys
import threading
import time
import types
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from loguru import logger

from src.services.bot.livelog import (
    TELEGRAM_TEXT_LIMIT,
    LiveLogMessage,
    LiveRunPublisher,
    LogBuffer,
    RunProgress,
    ShopStatus,
    _make_filter,
    _short_url,
    install_sink,
    render,
    render_failure_report,
)
from src.services.discovery.sitemap import DiscoveryResult


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
        # NOTE: never import the real module here -- it authenticates against
        # Google at import time. A duck-typed stand-in is enough: the monitor
        # only touches .url and .sheet_mark.
        @dataclass
        class CompetitorRow:
            url: str
            sheet_mark: bool = False

        stub = types.ModuleType("src.services.gsheets")

        class _StubGSheetsService:
            def get_competitor_rows(self):
                return [
                    CompetitorRow(url=url, sheet_mark=url.endswith("bad.example"))
                    for url in _STUB["competitors"]
                ]

            def get_competitors(self):
                return list(_STUB["competitors"])

            def log_new_collection(self, *args, **kwargs):
                return None

        stub.gsheets_service = _StubGSheetsService()
        sys.modules["src.services.gsheets"] = stub

    import src.services.monitor as monitor_module

    return monitor_module


_STUB = {"competitors": []}


class _InMemoryHealthStore:
    """Keeps the health DB out of the developer's real tracker.db."""

    def __init__(self, *args, **kwargs):
        self.data: dict = {}

    def record_run(self, results, now=None):
        for item in results:
            url = item["url"]
            rec = self.data.setdefault(url, {"fails_streak": 0, "runs": 0})
            rec["runs"] += 1
            if item.get("failed"):
                rec["fails_streak"] += 1
                rec["last_detail"] = item.get("detail")
            else:
                rec["fails_streak"] = 0
                rec["last_detail"] = None
                rec["last_ok_at"] = 1.0
        return self.data

    def dead(self, threshold=3):
        return [
            {"url": url, **rec}
            for url, rec in self.data.items()
            if rec.get("fails_streak", 0) >= threshold
        ]


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
    # The cut line must not claim failures are all above: some can be below,
    # and the follow-up report holds the complete list.
    assert "полный список в отчёте об ошибках" in text
    assert "ошибки — вверху" not in text


def test_cut_line_states_how_many_failures_did_not_fit():
    """Never let a failure disappear silently behind a misleading label."""
    progress = RunProgress()
    progress.set_total(400)
    for i in range(400):
        url = f"https://shop-{i:03d}.example.com"
        progress.begin(url)
        if i < 150:
            progress.finish(url, "failed", 0, "403 — блокируют бота")
        else:
            progress.finish(url, "ok", 1)
    progress.finish_run()

    text = render(progress, LogBuffer(maxlen=10))
    combined = text + "\n".join(render_failure_report(progress, LogBuffer(maxlen=10)))

    # Count listed shop rows only -- the "Причины" summary repeats the reason
    # text once and would throw the count off by one.
    listed = sum(1 for line in text.splitlines() if line.startswith("❌ "))
    hidden_failures = 150 - listed
    assert hidden_failures > 0, "scenario must actually overflow"
    assert f"из них {hidden_failures} с ошибками" in text
    # ...and every one of them is recoverable from the report message.
    for i in range(150):
        assert f"shop-{i:03d}.example.com" in combined


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


# --- run-level failure and dead-site shortlist ------------------------


def test_run_error_is_rendered_instead_of_a_frozen_placeholder():
    progress = RunProgress()
    progress.run_error = "Google Sheets: Invalid JWT Signature — ключ отозван"
    text = render(progress, LogBuffer(maxlen=10))
    assert "Парсинг прерван" in text
    assert "Invalid JWT Signature" in text


def test_trigger_line_is_shown():
    progress = RunProgress()
    progress.trigger = "по расписанию, 08:00 МСК"
    assert "по расписанию, 08:00 МСК" in render(progress, LogBuffer(maxlen=10))


def test_dead_section_lists_streaking_sites():
    progress = RunProgress()
    progress.shops = [
        ShopStatus(url="velana.ua", state="failed", error="сервер не отвечает",
                   fails_streak=4, last_ok_at=time.time() - 86400 * 3),
        ShopStatus(url="anthropologie.com", state="failed",
                   error="403 — блокируют бота", fails_streak=3,
                   last_ok_at=time.time() - 86400 * 9),
    ]
    progress.dead = list(progress.shops)
    progress.finish_run()

    text = render(progress, LogBuffer(maxlen=10))
    assert "Падают подряд" in text
    assert "velana.ua" in text
    assert "сервер не отвечает" in text
    assert "anthropologie.com" in text


def test_sheet_mark_is_shown_without_changing_the_status():
    progress = RunProgress()
    progress.begin("https://berta.com", sheet_mark=True)
    progress.finish("https://berta.com", "failed", 0, "403 — блокируют бота")
    text = render(progress, LogBuffer(maxlen=10))
    assert "⛔" in text
    assert "❌" in text  # the mark is additive, not a replacement status
    assert progress.marked_count == 1


def test_http_status_is_appended_when_not_already_present():
    progress = RunProgress()
    progress.begin("https://x.example")
    progress.finish("https://x.example", "failed", 0, "403 — блокируют бота",
                    http_status=403)
    text = render(progress, LogBuffer(maxlen=10))
    assert text.count("403") == 1  # not duplicated

    progress2 = RunProgress()
    progress2.begin("https://y.example")
    progress2.finish("https://y.example", "failed", 0, "таймаут ответа",
                     http_status=503)
    assert "HTTP 503" in render(progress2, LogBuffer(maxlen=10))


def test_truncated_sites_are_flagged():
    progress = RunProgress()
    progress.begin("https://davidsbridal.com")
    progress.finish("https://davidsbridal.com", "ok", 3, truncated=True)
    text = render(progress, LogBuffer(maxlen=10))
    assert "много URL" in text
    assert progress.truncated_count == 1


# --- LiveRunPublisher: one run, many admins -----------------------------


class _ParkingSleep:
    """Parks forever so only the test drives the publisher's tick loop.

    With interval=0 and a yielding sleep the publisher's own loop would spin
    and tick concurrently with a manual tick(), which is a test artefact, not
    production behaviour (production sleeps 3 real seconds between ticks).

    It is wired in as `loop_sleep` only. The send throttle keeps the fake
    clock's sleep: retry_after / min_gap waits are real control flow and must
    still complete (and be recorded), otherwise a blocked-chat test parks
    inside error handling instead of asserting.
    """

    async def __call__(self, _seconds):
        await asyncio.Event().wait()


def _publisher(bot, chats=(1, 2, 3)):
    clock = _FakeClock()
    bot.clock = clock
    return (
        LiveRunPublisher(
            bot,
            list(chats),
            buffer=LogBuffer(maxlen=200),
            interval=0.0,
            min_gap=0.0,
            clock=clock,
            sleep=clock.sleep,
            loop_sleep=_ParkingSleep(),
        ),
        clock,
    )


def test_publisher_sends_one_message_per_admin():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 222, 333))

    async def scenario():
        await pub.start()
        await pub.stop()

    asyncio.run(scenario())
    sends = [c for c in bot.calls if c["kind"] == "send"]
    assert [c["chat_id"] for c in sends] == [111, 222, 333]


def test_publisher_edits_every_subscriber_with_the_same_text():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 222))

    async def scenario():
        await pub.start()
        pub.progress.set_total(2)
        pub.progress.begin("https://a.example")
        pub.buffer.append("line one")
        await pub.tick()
        await pub.stop()

    asyncio.run(scenario())
    edits = [c for c in bot.calls if c["kind"] == "edit"]
    by_chat: dict = {}
    for e in edits:
        by_chat.setdefault(e["chat_id"], []).append(e["text"])
    assert set(by_chat) == {111, 222}
    assert by_chat[111][-1] == by_chat[222][-1]  # identical text
    assert "line one" in by_chat[111][-1]


def test_publisher_renders_once_per_tick_for_all_chats():
    renders = {"n": 0}
    import src.services.bot.livelog as livelog_mod

    original = livelog_mod.render

    def counting(progress, buffer, **kwargs):
        renders["n"] += 1
        return original(progress, buffer, **kwargs)

    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 222, 333))

    async def scenario():
        await pub.start()
        pub.buffer.append("only line")
        renders["n"] = 0
        livelog_mod.render = counting
        try:
            await pub.tick()
        finally:
            livelog_mod.render = original
        await pub.stop()

    asyncio.run(scenario())
    assert renders["n"] == 1  # one render, three chats


def test_publisher_skips_the_tick_when_nothing_changed():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 222))

    async def scenario():
        await pub.start()
        await pub.tick()
        first = len([c for c in bot.calls if c["kind"] == "edit"])
        await pub.tick()  # no new log lines, no state change
        second = len([c for c in bot.calls if c["kind"] == "edit"])
        await pub.stop()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second


def test_one_broken_subscriber_does_not_stop_the_others():
    error = TelegramRetryAfter(
        method=None, message="Too Many Requests: retry after 5", retry_after=5
    )
    bot = _FakeBot()
    pub, clock = _publisher(bot, (111, 222))

    original_edit = bot.edit_message_text

    async def flaky(text, chat_id=None, message_id=None, parse_mode=None):
        if chat_id == 111:
            raise error
        return await original_edit(
            text, chat_id=chat_id, message_id=message_id, parse_mode=parse_mode
        )

    bot.edit_message_text = flaky

    async def scenario():
        await pub.start()
        pub.buffer.append("shared line")
        await pub.tick()
        await pub.stop()

    asyncio.run(scenario())

    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert [c["chat_id"] for c in edits] == [222]  # 222 was not blocked
    assert any(s == 5.5 for s in clock.sleeps)  # retry_after + jitter


def test_publisher_fail_run_shows_in_every_chat():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 222))

    async def scenario():
        await pub.start()
        pub.fail_run("Google Sheets: отозван ключ")  # failure happens mid-run
        await pub.tick()
        await pub.stop()

    asyncio.run(scenario())
    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert edits
    for e in edits:
        assert "отозван ключ" in e["text"]


def test_publisher_keeps_the_trigger_set_before_start():
    """trigger describes the run, so reset() must not wipe it."""
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111,))
    pub.progress.trigger = "по расписанию, 08:00 МСК"

    async def scenario():
        await pub.start()
        await pub.tick()
        await pub.stop()

    asyncio.run(scenario())
    edits = [c for c in bot.calls if c["kind"] == "edit"]
    assert "по расписанию" in edits[-1]["text"]


def test_publisher_deduplicates_subscribers():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 111, 222))
    assert list(pub.subscribers) == [111, 222]


# --- monitor integration ------------------------------------------------


def test_run_check_cycle_continues_after_site_failure(monkeypatch):
    monitor_module = _load_monitor()
    monkeypatch.setattr(monitor_module, "ShopHealthStore", _InMemoryHealthStore)
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
        monitor_module.discovery,
        "discover_urls_detailed",
        lambda url, **kwargs: DiscoveryResult(urls=[], ok=False, code="dns", detail="домен не резолвится"),
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    result = asyncio.run(
        monitor_module.monitor_service.check_competitor("https://empty.example.com")
    )

    assert result["status"] == "failed"
    assert result["fail_code"] == "dns"
    assert result["error"] == "домен не резолвится"
    assert result["new_urls"] == 0


# --- failure report: every failed site must be visible ------------------
#
# The bug this guards: with 50+ failures the single live message ran out of
# characters and dropped sites *silently*, so the user could not see which
# sites to investigate. The report is the fix, so the invariant is "no
# failure disappears anywhere in the output".


def _run_with_failures(fails, ok_count=250):
    """Build a finished run: `fails` is [(url, reason, http_status)]."""
    buffer = LogBuffer(maxlen=2000)
    progress = RunProgress()
    progress.trigger = "ручной запуск"
    progress.set_total(len(fails) + ok_count)
    for url, reason, status in fails:
        progress.begin(url)
        progress.finish(url, "failed", error=reason, http_status=status)
    for i in range(ok_count):
        url = f"https://salon-{i:03d}.example"
        progress.begin(url)
        progress.finish(url, "ok", new_urls=2)
    for i in range(400):  # INFO spam, as a real run produces
        buffer.append(f"16:0{i % 10}:01 | INFO     | check_competitor:{i} - page {i}")
    progress.finish_run()
    return progress, buffer


def _many_failures(count=55):
    real = [
        ("https://shlomitazrad.com", "таймаут ответа", None),
        ("https://instagram.com/ronalinabridal", "главная без ссылок (JS-сайт)", 200),
        ("https://saintbridalcouture.com", "403 — блокируют бота", 403),
        ("https://agnesfashiongroup.com", "ошибка TLS/сертификата", None),
        ("https://candelasyfelipa.es", "429 — нас ограничивают", 429),
    ]
    fails = list(real)
    fails += [
        (f"https://boutique-{i:02d}.co.uk", "403 — блокируют бота", 403)
        for i in range(count - len(real))
    ]
    return fails


def test_report_shows_every_failed_site_for_a_realistic_run():
    fails = _many_failures()
    progress, buffer = _run_with_failures(fails)

    chunks = render_failure_report(progress, buffer)
    combined = "\n".join(chunks)

    missing = [
        url
        for url, _, _ in fails
        if _short_url(url) not in combined
    ]
    assert not missing, f"failed sites invisible to the user: {missing}"
    assert len(chunks) >= 1


def test_report_never_exceeds_the_telegram_limit():
    fails = _many_failures(200)  # pathological: forces the multi-message path
    progress, buffer = _run_with_failures(fails)
    for chunk in render_failure_report(progress, buffer):
        assert len(chunk) <= TELEGRAM_TEXT_LIMIT


def test_report_fits_a_realistic_run_into_a_single_message():
    fails = _many_failures()
    progress, buffer = _run_with_failures(fails)
    assert len(render_failure_report(progress, buffer)) == 1


def test_report_groups_repeated_reasons_instead_of_repeating_them():
    """45 identical rows carry one bit; the grouped form does not hide sites."""
    fails = _many_failures()
    progress, buffer = _run_with_failures(fails)
    text = "\n".join(render_failure_report(progress, buffer))

    assert "403 — блокируют бота" in text
    assert "таймаут ответа" in text
    assert "instagram.com/ronalinabridal" in text  # listed, not just counted


def test_report_is_empty_when_nothing_failed():
    progress, buffer = _run_with_failures([], ok_count=10)
    assert render_failure_report(progress, buffer) == []


def test_main_message_carries_the_reason_summary():
    fails = _many_failures()
    progress, buffer = _run_with_failures(fails)
    text = render(progress, buffer)

    assert "Причины:" in text
    assert "403 — блокируют бота" in text


def test_report_prefers_warning_logs_over_info_spam():
    fails = [("https://a.example.com", "403 — блокируют бота", 403)]
    progress, buffer = _run_with_failures(fails)
    for i in range(200):
        buffer.append(f"16:00:01 | INFO     | noop:{i} - noise")
    buffer.append("16:00:02 | WARNING  | monitor:real problem here")

    text = "\n".join(render_failure_report(progress, buffer))
    assert "real problem" in text
    assert "noise" not in text


def test_report_flags_sites_failing_many_runs_in_a_row():
    progress, buffer = _run_with_failures(
        [("https://dead-bride.com", "домен не резолвится", None)]
    )
    for shop in progress.shops:
        if "dead-bride" in shop.url:
            shop.fails_streak = 5
    text = "\n".join(render_failure_report(progress, buffer))
    assert "падает 5 раз подряд" in text


def test_publisher_sends_the_report_to_every_admin():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111, 222))

    async def scenario():
        await pub.start()
        pub.progress.set_total(2)
        pub.progress.begin("https://broken.example.com")
        pub.progress.finish(
            "https://broken.example.com", "failed", error="403 — блокируют бота"
        )
        await pub.tick()
        await pub.stop()

    asyncio.run(scenario())

    sends = [c for c in bot.calls if c["kind"] == "send"]
    reports = [
        c for c in sends if "Ошибки парсинга" in c["text"]
    ]
    assert [c["chat_id"] for c in reports] == [111, 222]
    assert "broken.example.com" in reports[0]["text"]


def test_publisher_sends_no_report_when_the_run_was_clean():
    bot = _FakeBot()
    pub, _ = _publisher(bot, (111,))

    async def scenario():
        await pub.start()
        pub.progress.set_total(1)
        pub.progress.begin("https://fine.example.com")
        pub.progress.finish("https://fine.example.com", "ok", new_urls=1)
        await pub.tick()
        await pub.stop()

    asyncio.run(scenario())

    sends = [c for c in bot.calls if c["kind"] == "send"]
    assert not [c for c in sends if "Ошибки парсинга" in c["text"]]
