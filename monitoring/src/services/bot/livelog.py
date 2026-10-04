"""Live-updating Telegram log message for a parsing run (парсинг прогон).

One message is sent once and then rewritten in place with `editMessageText`,
mirroring the terminal log so the operator can see *which shop is being
processed right now* and *which shops failed*.

Telegram constraints this module is built around (see plan §7):
  * ~1 message/second per chat, ~20 messages/minute per group -> edits are
    throttled, never bursted.
  * `editMessageText` allows 1-4096 characters **after entity parsing**; the
    renderer budgets on the *escaped* string, which is always >= the parsed
    length, so the cap is conservative and always respected.
  * An edit with identical content and markup returns
    `400 message is not modified` -> identical renders are never sent.
  * A 429 must be answered with exactly `retry_after` seconds of sleep.

Ownership / concurrency:
  * `LogBuffer` is written by the loguru sink, which runs on whatever thread
    logged -- including `asyncio.to_thread` workers -- so it uses a
    `threading.Lock`, not asyncio primitives.
  * `RunProgress` is written by the single parsing coroutine and read by the
    render task, both on the event loop, so it needs no lock. `snapshot()`
    hands out a copy so a render is never torn mid-update.
  * `LiveLogMessage` is the only owner of the message id. Business code never
    touches the message: it is a projection of (buffer + progress).
"""
from __future__ import annotations

import asyncio
import html
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from loguru import logger

# Telegram limits
TELEGRAM_TEXT_LIMIT = 4096
_RENDER_TARGET = 4000  # stay below the hard cap, leaves room for the final edit

# Refresh cadence
_PRIVATE_INTERVAL = 3.0
_GROUP_INTERVAL = 4.0  # 20 msgs/min per group => >= 3.1s
_GROUP_TYPES = ("group", "supergroup", "channel")
_HARD_MIN_GAP = 1.05  # never hit a chat faster than ~1 msg/sec

# Render tuning
_LIVE_SHOP_LINES = 12  # while running: keep it short, the log tail matters more
_FINAL_SHOP_LINES = 80  # finished report: show the failures, that is the point
_MIN_TAIL_LINES = 12  # but never let the shop list starve the log tail
_MAX_URL_CHARS = 40
_MAX_LOG_LINE = 300
_MAX_TAIL_LINES = 600

_DEFAULT_DENY = (
    "aiogram",
    "aiohttp",
    "asyncio",
    "apscheduler",
    "src.services.bot.livelog",
)

_ICONS = {
    "ok": "✅",
    "failed": "❌",
    "running": "⏳",
    "pending": "⏸️",
    "skipped": "⏭️",
}


# --------------------------------------------------------------------------
# Log buffer
# --------------------------------------------------------------------------
class LogBuffer:
    """Bounded, thread-safe ring buffer of rendered log lines."""

    def __init__(self, maxlen: int = 2000):
        self._lines: deque[str] = deque(maxlen=max(1, maxlen))
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        """Never raises -- a logging sink must not break the caller."""
        try:
            with self._lock:
                self._lines.append(line)
        except Exception:
            pass

    def tail(self, n: int = 0) -> list[str]:
        """Newest-last slice. ``n <= 0`` returns everything."""
        with self._lock:
            items = list(self._lines)
        return items[-n:] if n > 0 else items

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)


_buffer: LogBuffer | None = None
_buffer_lock = threading.Lock()


def get_buffer() -> LogBuffer:
    """Process-wide buffer shared by the loguru sink and the live message."""
    global _buffer
    if _buffer is None:
        with _buffer_lock:
            if _buffer is None:
                from src.config import settings

                _buffer = LogBuffer(maxlen=settings.TELEGRAM_LIVE_LOG_BUFFER)
    return _buffer


# --------------------------------------------------------------------------
# Structured run progress
# --------------------------------------------------------------------------
@dataclass
class ShopStatus:
    url: str
    state: str = "pending"  # pending | running | ok | failed | skipped
    new_urls: int = 0
    error: str | None = None
    duration: float = 0.0
    started_at: float | None = None


class RunProgress:
    """Per-run structured state. Single-writer (the parsing coroutine)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.total = 0
        self.index = 0
        self.current = ""
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.shops: list[ShopStatus] = []

    # -- mutators ---------------------------------------------------------
    def set_total(self, total: int) -> None:
        self.total = int(total)

    def begin(self, url: str) -> ShopStatus:
        self.current = url
        self.index = len(self.shops) + 1
        shop = self._slot(url)
        shop.state = "running"
        shop.started_at = time.monotonic()
        return shop

    def finish(
        self,
        url: str,
        state: str,
        new_urls: int = 0,
        error: str | None = None,
    ) -> ShopStatus:
        shop = self._slot(url)
        shop.state = state
        shop.new_urls = new_urls
        shop.error = error
        if shop.started_at is not None:
            shop.duration = time.monotonic() - shop.started_at
        if url == self.current:
            self.current = ""
        return shop

    def finish_run(self) -> None:
        self.finished_at = time.monotonic()
        self.current = ""

    def _slot(self, url: str) -> ShopStatus:
        for shop in reversed(self.shops):
            if shop.url == url:
                return shop
        shop = ShopStatus(url=url)
        self.shops.append(shop)
        return shop

    # -- derived ----------------------------------------------------------
    @property
    def ok_count(self) -> int:
        return sum(1 for s in self.shops if s.state == "ok")

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.shops if s.state == "failed")

    @property
    def skipped_count(self) -> int:
        return sum(1 for s in self.shops if s.state == "skipped")

    @property
    def total_new(self) -> int:
        return sum(s.new_urls for s in self.shops if s.state == "ok")

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at

    def snapshot(self) -> "RunProgress":
        copy = RunProgress()
        copy.total = self.total
        copy.index = self.index
        copy.current = self.current
        copy.started_at = self.started_at
        copy.finished_at = self.finished_at
        copy.shops = [ShopStatus(**vars(s)) for s in self.shops]
        return copy


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _short_url(url: str, limit: int = _MAX_URL_CHARS) -> str:
    text = re.sub(r"^https?://", "", (url or "").strip(), flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    text = text.rstrip("/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    if seconds < 60:
        return f"{seconds}с"
    return f"{seconds // 60}м {seconds % 60:02d}с"


def _header(progress: RunProgress) -> str:
    total = progress.total or len(progress.shops)
    done = progress.finished_at is not None
    title = "✅ <b>Парсинг завершён</b>" if done else "🩺 <b>Парсинг конкурентов</b>"
    if total:
        title += f" · {progress.index or len(progress.shops)}/{total}"
    if progress.current:
        title += f" · {html.escape(_short_url(progress.current, 28))}"
    counters = f"✅ {progress.ok_count} · ❌ {progress.failed_count}"
    # Baseline-skipped sites are neither ok nor failed; without this counter a
    # first run reads as "everything failed" even when 260 sites were indexed.
    if progress.skipped_count:
        counters += f" · ⏭️ {progress.skipped_count}"
    if progress.total_new:
        counters += f" · 🆕 {progress.total_new}"
    return f"{title}\n⏱ {_fmt_duration(progress.elapsed)}   {counters}"


def _shop_line(shop: ShopStatus) -> str:
    icon = _ICONS.get(shop.state, "⏸️")
    name = html.escape(_short_url(shop.url))
    if shop.state == "ok":
        detail = f"{shop.new_urls} новых" if shop.new_urls else "без новых"
    elif shop.state == "failed":
        detail = html.escape(shop.error or "ошибка")
    elif shop.state == "running":
        detail = "сканирую…"
    elif shop.state == "skipped":
        detail = "первый запуск, база заполнена"
    else:
        detail = "ждёт"
    duration = f" ({_fmt_duration(shop.duration)})" if shop.duration >= 1 else ""
    return f"{icon} {name} — {detail}{duration}"


def _shop_selection(
    shops: Sequence[ShopStatus], max_lines: int
) -> tuple[list[ShopStatus], int]:
    """Pick which shops to list, and report how many are hidden.

    Failures win the budget. This view exists to answer "which shops did not
    parse", and with a 300+ competitor list the failures are the oldest entries
    -- a plain "last N" window would hide every single one of them.
    """
    if len(shops) <= max_lines:
        return list(shops), 0

    failed = [s for s in shops if s.state == "failed"]
    others = [s for s in shops if s.state != "failed"]

    shown: list[ShopStatus] = []
    budget = max_lines - 1  # reserve a line for the "… ещё N" marker
    for group in (failed, others):
        if budget <= 0:
            break
        take = group[-budget:]
        shown.extend(take)
        budget -= len(take)
    return shown, len(shops) - len(shown)


def _compose(
    progress: RunProgress,
    shown: Sequence[ShopStatus],
    hidden: int,
    tail_lines: Sequence[str],
    dropped: int,
) -> str:
    blocks = [_header(progress)]

    if progress.shops:
        lines = [_shop_line(s) for s in shown]
        if hidden:
            lines.insert(0, f"… ещё {hidden} магазинов (ошибки — вверху)")
        blocks.append("<b>Магазины:</b>\n" + "\n".join(lines))

    if tail_lines:
        label = "<b>Логи:</b>"
        if dropped:
            label += f" (ещё {dropped} строк выше)"
        blocks.append(label + "\n" + "\n".join(tail_lines))

    return "\n\n".join(blocks)


def render(
    progress: RunProgress,
    buffer: LogBuffer,
    *,
    max_chars: int = TELEGRAM_TEXT_LIMIT,
    max_shop_lines: int | None = None,
) -> str:
    """Build the message text. Never exceeds ``max_chars`` (measured escaped).

    While a run is in progress the shop list stays short (the log tail is the
    interesting part then). Once the run is finished it expands, because the
    finished report should show every failure rather than a 12-line window.
    """
    if max_shop_lines is None:
        max_shop_lines = (
            _FINAL_SHOP_LINES if progress.finished_at is not None else _LIVE_SHOP_LINES
        )

    shown, hidden = _shop_selection(progress.shops, max_shop_lines)
    all_lines = [html.escape(line) for line in buffer.tail(_MAX_TAIL_LINES)]
    tail_lines = list(all_lines)
    dropped = 0

    while True:
        text = _compose(progress, shown, hidden, tail_lines, dropped)
        if len(text) <= max_chars:
            return text
        # Shed the log tail first, but keep a floor of it: a finished report
        # made of shop lines only loses the context that explains them.
        if len(tail_lines) > _MIN_TAIL_LINES:
            tail_lines.pop(0)
            dropped += 1
            continue
        if shown:
            shown.pop()  # oldest non-failure line; failures are kept
            hidden += 1
            continue
        if tail_lines:
            tail_lines.pop(0)
            dropped += 1
            continue
        return text[:max_chars]


# --------------------------------------------------------------------------
# Live message
# --------------------------------------------------------------------------
class LiveLogMessage:
    """Owns one Telegram message and rewrites it while a run is in progress."""

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        buffer: LogBuffer,
        progress: RunProgress | None = None,
        *,
        interval: float | None = None,
        min_gap: float = _HARD_MIN_GAP,
        chat_type: str = "private",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.buffer = buffer
        self.progress = progress if progress is not None else RunProgress()
        self.interval = (
            interval
            if interval is not None
            else (_GROUP_INTERVAL if chat_type in _GROUP_TYPES else _PRIVATE_INTERVAL)
        )
        self.min_gap = min_gap
        self.chat_type = chat_type

        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._last_text = ""
        self._last_call: float | None = None
        self._parse_mode: str | None = "HTML"
        self._broken = False
        self._penalty = 1
        self._seen_errors: set[str] = set()

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> int:
        """Send the message and start the refresh task. Returns message_id."""
        self.buffer.clear()
        self.progress.reset()
        placeholder = "🩺 Запускаю парсинг…"
        # aiogram's contract: send_message(chat_id, text, ...) -- chat_id FIRST.
        # (edit_message_text is the other way round; see _edit.)
        sent = await self.bot.send_message(
            self.chat_id, placeholder, parse_mode=self._parse_mode
        )
        self.message_id = sent.message_id
        self._last_text = placeholder
        self._last_call = self._clock()
        self._task = asyncio.create_task(self._loop())
        return self.message_id

    async def _loop(self) -> None:
        while not self._broken:
            await self._sleep(self.interval * self._penalty)
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note_error("live-log flush", exc)

    async def stop(self, final_text: str | None = None) -> None:
        """Stop refreshing and leave the finished report in the chat."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._broken or getattr(self, "message_id", None) is None:
            return
        text = final_text or render(self.progress, self.buffer)
        if text == self._last_text:
            return
        try:
            await self._gate()
            await self._edit(text)
            self._last_text = text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_error(exc, text)

    # -- internals --------------------------------------------------------
    async def _flush(self) -> None:
        if self._broken or getattr(self, "message_id", None) is None:
            return
        text = render(self.progress, self.buffer)
        if text == self._last_text:
            return  # no-op edit is a 400 on Telegram
        await self._gate()
        try:
            await self._edit(text)
            self._last_text = text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_error(exc, text)

    async def _gate(self) -> None:
        """Telegram allows ~1 message/sec per chat; never exceed that."""
        if self._last_call is not None:
            wait = self._last_call + self.min_gap - self._clock()
            if wait > 0:
                await self._sleep(wait)
        self._last_call = self._clock()

    async def _edit(self, text: str) -> None:
        # aiogram's contract: edit_message_text(text, ..., chat_id=, message_id=)
        # -- text comes FIRST here, unlike send_message(chat_id, text).
        if self._parse_mode is None:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=self.message_id
            )
        else:
            await self.bot.edit_message_text(
                text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode=self._parse_mode,
            )

    async def _handle_error(self, exc: Exception, text: str) -> None:
        """Every Telegram failure mode, handled so a run never breaks."""
        desc = str(exc)

        if isinstance(exc, TelegramRetryAfter) or "retry after" in desc.lower():
            retry_after = float(getattr(exc, "retry_after", 1) or 1)
            await self._sleep(retry_after + 0.5)  # exact retry_after + jitter
            self._penalty = max(self._penalty, 2)  # self-throttle the rest of the run
            self._last_call = self._clock()
            try:
                await self._edit(text)
                self._last_text = text
            except asyncio.CancelledError:
                raise
            except Exception as retry_exc:
                self._note_error("retry after edit failed", retry_exc)
            return

        if isinstance(exc, TelegramBadRequest) or "Bad Request" in desc:
            if "message is not modified" in desc:
                self._last_text = text  # already showing this
                return
            if "can't parse entities" in desc:
                self._parse_mode = None
                plain = html.unescape(text)
                try:
                    await self._edit(plain)
                    self._last_text = plain
                except asyncio.CancelledError:
                    raise
                except Exception as plain_exc:
                    self._note_error("plain-text fallback failed", plain_exc)
                return
            if (
                "message can't be edited" in desc
                or "message to edit not found" in desc
                or "MESSAGE_ID_INVALID" in desc
            ):
                self._broken = True
                self._note_error("live log disabled (message uneditable)", exc)
                return

        self._note_error("live-log edit failed", exc)

    def _note_error(self, context: str, exc: Exception) -> None:
        """Log once per distinct error -- the sink filters our own records."""
        key = f"{context}:{type(exc).__name__}:{exc}"
        if key in self._seen_errors:
            return
        self._seen_errors.add(key)
        logger.warning(f"{context}: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# loguru sink
# --------------------------------------------------------------------------
_sink_state = threading.local()


def _make_filter(deny: Sequence[str]) -> Callable[[dict], bool]:
    prefixes = tuple(p for p in deny if p)

    def _filter(record: dict) -> bool:
        # Re-entrancy: never let a record raised by the sink itself loop back.
        if getattr(_sink_state, "active", False):
            return False
        name = record.get("name") or ""
        for prefix in prefixes:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True

    return _filter


def install_sink(
    buffer: LogBuffer,
    level: str = "INFO",
    enabled: bool = True,
    deny: Sequence[str] = _DEFAULT_DENY,
) -> int | None:
    """Mirror the terminal log stream into ``buffer``. Returns the handler id."""
    if not enabled:
        return None

    def _sink(message) -> None:
        _sink_state.active = True
        try:
            record = message.record
            line = (
                f"{record['time']:%H:%M:%S} | {record['level']:<7} | "
                f"{record['message']}"
            )
            exc = record.get("exception")
            if exc is not None:
                # One line only -- the full traceback stays in logs/app.log.
                line += f"  ↳ {exc.type.__name__}: {exc.value}"
            if len(line) > _MAX_LOG_LINE:
                line = line[: _MAX_LOG_LINE - 1] + "…"
            buffer.append(line)
        except Exception:
            pass
        finally:
            _sink_state.active = False

    return logger.add(
        _sink,
        level=level,
        colorize=False,
        backtrace=False,
        filter=_make_filter(deny),
    )
