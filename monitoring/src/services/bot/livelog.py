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

# Failure report (sent as follow-up messages after the run finishes)
_REPORT_TARGET = 3900  # per-message budget, under TELEGRAM_TEXT_LIMIT
_REPORT_MAX_PER_CHUNK = 45  # keeps the list scannable, not just legal
_REPORT_HEADERS = (
    "🩺 Парсинг конкурентов",
    "🔍 Причины ошибок",
)
# Mirrors health.DEAD_AFTER_RUNS without importing the storage layer into the
# bot: this module must stay importable on its own (tests import it directly).
_STREAK_HIGHLIGHT = 3
# Levels worth repeating in the report: the INFO spam is what made the old
# message unreadable (it was 30% of the budget and said nothing).
_REPORT_LOG_LEVELS = ("WARNING", "ERROR", "CRITICAL")
_MAX_REPORT_LOG_LINES = 15

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
    fail_code: str | None = None
    http_status: int | None = None
    sheet_mark: bool = False  # operator wrote "не партся" in the sheet (display only)
    truncated: bool = False  # hit the sitemap URL cap
    fails_streak: int = 0
    last_ok_at: float | None = None


class RunProgress:
    """Per-run structured state. Single-writer (the parsing coroutine)."""

    def __init__(self) -> None:
        self.trigger: str = ""  # "ручной" | "по расписанию" — survives reset
        self.reset()

    def reset(self) -> None:
        self.total = 0
        self.index = 0
        self.current = ""
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.shops: list[ShopStatus] = []
        self.run_error: str | None = None
        self.dead: list[ShopStatus] = []
        # `trigger` is set by the caller *before* start() (manual vs cron), so
        # it deliberately survives a reset.

    # -- mutators ---------------------------------------------------------
    def set_total(self, total: int) -> None:
        self.total = int(total)

    def begin(self, url: str, *, sheet_mark: bool = False) -> ShopStatus:
        self.current = url
        self.index = len(self.shops) + 1
        shop = self._slot(url)
        shop.state = "running"
        shop.started_at = time.monotonic()
        shop.sheet_mark = sheet_mark
        return shop

    def finish(
        self,
        url: str,
        state: str,
        new_urls: int = 0,
        error: str | None = None,
        *,
        fail_code: str | None = None,
        http_status: int | None = None,
        truncated: bool = False,
        fails_streak: int = 0,
        last_ok_at: float | None = None,
    ) -> ShopStatus:
        shop = self._slot(url)
        shop.state = state
        shop.new_urls = new_urls
        shop.error = error
        shop.fail_code = fail_code
        shop.http_status = http_status
        shop.truncated = truncated
        shop.fails_streak = fails_streak
        shop.last_ok_at = last_ok_at
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
    def marked_count(self) -> int:
        return sum(1 for s in self.shops if s.sheet_mark)

    @property
    def truncated_count(self) -> int:
        return sum(1 for s in self.shops if s.truncated)

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
        copy.run_error = self.run_error
        copy.trigger = self.trigger
        copy.shops = [ShopStatus(**vars(s)) for s in self.shops]
        copy.dead = [ShopStatus(**vars(s)) for s in self.dead]
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
    if progress.run_error:
        title = "⛔ <b>Парсинг прерван</b>"
    elif done:
        title = "✅ <b>Парсинг завершён</b>"
    else:
        title = "🩺 <b>Парсинг конкурентов</b>"
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
    tail = "   " + progress.trigger if progress.trigger else ""
    return f"{title}\n⏱ {_fmt_duration(progress.elapsed)}   {counters}{tail}"


def _shop_line(shop: ShopStatus) -> str:
    icon = _ICONS.get(shop.state, "⏸️")
    name = html.escape(_short_url(shop.url))
    if shop.state == "ok":
        detail = f"{shop.new_urls} новых" if shop.new_urls else "без новых"
    elif shop.state == "failed":
        detail = html.escape(shop.error or "ошибка")
        if shop.http_status and f"{shop.http_status}" not in detail:
            detail += f" (HTTP {shop.http_status})"
    elif shop.state == "running":
        detail = "сканирую…"
    elif shop.state == "skipped":
        detail = "первый запуск, база заполнена"
    else:
        detail = "ждёт"
    if shop.truncated:
        detail += " · много URL"
    duration = f" ({_fmt_duration(shop.duration)})" if shop.duration >= 1 else ""
    # ⛔ = the operator marked it "не партся" in the sheet. Display only.
    mark = " ⛔" if shop.sheet_mark else ""
    return f"{icon} {name} — {detail}{duration}{mark}"


def _dead_section(progress: RunProgress, max_lines: int = 8) -> str:
    """Sites that failed N runs in a row — the actionable shortlist."""
    if not progress.dead:
        return ""
    shown = progress.dead[-max_lines:]
    lines = [f"⚠️ <b>Падают подряд:</b> {len(progress.dead)}"]
    for shop in shown:
        since = ""
        if shop.last_ok_at:
            since = f" (успех {time.strftime('%d.%m', time.localtime(shop.last_ok_at))})"
        detail = html.escape(shop.error or shop.fail_code or "ошибка")
        lines.append(f" • {html.escape(_short_url(shop.url))} — {detail}{since}")
    if len(progress.dead) > len(shown):
        lines.insert(1, f"… ещё {len(progress.dead) - len(shown)}")
    return "\n".join(lines)


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

    if progress.run_error:
        blocks.append(f"❗ {html.escape(progress.run_error)}")

    # Reason summary: N failures become a handful of readable lines instead of
    # N near-identical ones, and it is the part that answers "why did it fail".
    summary = _reason_summary(progress)
    if summary and progress.finished_at is not None:
        blocks.append("<b>Причины:</b>\n" + "\n".join(summary))

    if progress.shops:
        lines = [_shop_line(s) for s in shown]
        if hidden:
            # Honest wording: some failures may be below the cut too, and the
            # report message holds the complete list. Saying "ошибки — вверху"
            # alone was how failures disappeared without anyone noticing.
            shown_urls = {s.url for s in shown}
            hidden_failures = sum(
                1
                for s in progress.shops
                if s.state == "failed" and s.url not in shown_urls
            )
            hint = f"… ещё {hidden} магазинов"
            if hidden_failures:
                hint += f", из них {hidden_failures} с ошибками"
            lines.insert(0, hint + " — полный список в отчёте об ошибках")
        blocks.append("<b>Магазины:</b>\n" + "\n".join(lines))

    dead = _dead_section(progress)
    if dead:
        blocks.append(dead)

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


def _reason_summary(progress: RunProgress, max_domains: int = 4) -> list[str]:
    """Group failures by reason: '403 — блокируют бота — 50 сайтов'.

    Fifty identical lines carry one bit of information. Grouping is what makes
    the summary readable *and* what frees the character budget for the full
    per-site list in the follow-up messages.
    """
    groups: dict[str, list[ShopStatus]] = {}
    for shop in progress.shops:
        if shop.state != "failed":
            continue
        reason = shop.error or shop.fail_code or "ошибка"
        groups.setdefault(reason, []).append(shop)

    lines = []
    for reason, shops in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        names = [_short_url(s.url) for s in shops]
        shown = names[:max_domains]
        more = len(names) - len(shown)
        tail = ", ".join(shown) + (f" и ещё {more}" if more > 0 else "")
        lines.append(f"• <b>{html.escape(reason)}</b> — {len(shops)}: {html.escape(tail)}")
    return lines


def _shop_detail(shop: ShopStatus) -> str:
    """`domain — reason (duration)` for one failure, no icon noise."""
    name = html.escape(_short_url(shop.url))
    reason = html.escape(shop.error or shop.fail_code or "ошибка")
    parts = [reason]
    if shop.http_status and str(shop.http_status) not in reason:
        parts.append(f"HTTP {shop.http_status}")
    detail = " · ".join(parts)
    if shop.truncated:
        detail += " · много URL"
    if shop.duration >= 1:
        detail += f" · {_fmt_duration(shop.duration)}"
    if shop.fails_streak >= _STREAK_HIGHLIGHT:
        detail += f" · падает {shop.fails_streak} раз подряд"
    return f"❌ {name} — {detail}"


def _grouped_detail(shops: Sequence[ShopStatus], wrap: int = 6) -> list[str]:
    """Compact per-reason domain lists for the report body.

    Forty-five identical '403' rows cost more characters than the names of the
    forty-five sites do. Grouping them turns the report from a wall of repeats
    into something a person can actually scan -- and it is what lets every
    failed site fit in a single message.
    """
    lines: list[str] = []
    extra_suffixes: dict[str, list[str]] = {}
    for shop in shops:
        reason = shop.error or shop.fail_code or "ошибка"
        note = ""
        if shop.fails_streak >= _STREAK_HIGHLIGHT:
            note = f"падает {shop.fails_streak} раз подряд"
        elif shop.truncated:
            note = "много URL"
        elif shop.duration >= 60:
            note = _fmt_duration(shop.duration)
        if note:
            extra_suffixes.setdefault(reason, []).append(
                f"{_short_url(shop.url)} ({note})"
            )

    groups: dict[str, list[str]] = {}
    for shop in shops:
        reason = shop.error or shop.fail_code or "ошибка"
        groups.setdefault(reason, []).append(_short_url(shop.url))

    for reason, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rows: list[str] = []
        for i in range(0, len(names), wrap):
            rows.append(", ".join(names[i : i + wrap]))
        body = "\n".join(rows)
        extra = extra_suffixes.get(reason) or []
        tail = f"\n  ↳ {', '.join(extra)}" if extra else ""
        lines.append(f"<b>{html.escape(reason)}</b> ({len(names)}):{tail}\n  {body}")

    return lines


def _relevant_log_lines(buffer: LogBuffer, limit: int = _MAX_REPORT_LOG_LINES) -> list[str]:
    """Only WARNING+ lines: the INFO stream is noise in a failure report."""
    wanted = tuple(f"| {lvl} " for lvl in _REPORT_LOG_LEVELS)
    picked = [line for line in buffer.tail(_MAX_TAIL_LINES) if any(w in line for w in wanted)]
    return [html.escape(line) for line in picked[-limit:]]


def render_failure_report(
    progress: RunProgress,
    buffer: LogBuffer,
    *,
    max_chars: int = _REPORT_TARGET,
    max_per_chunk: int = _REPORT_MAX_PER_CHUNK,
) -> list[str]:
    """Every failed site, split into as many Telegram messages as it takes.

    The single live message cannot hold 55 failures *and* stay useful, so the
    full list goes out as follow-ups. Returns an empty list when nothing
    failed, so a clean run sends nothing extra.
    """
    failed = [s for s in progress.shops if s.state == "failed"]
    if not failed:
        return []

    header = (
        f"🔍 <b>Ошибки парсинга</b> — {len(failed)} из "
        f"{progress.total or len(progress.shops)} сайтов"
    )
    log_tail = ""
    log_lines = _relevant_log_lines(buffer)
    if log_lines:
        extra = "<b>Логи (ошибки):</b>\n" + "\n".join(log_lines)
        log_tail = "\n\n" + extra

    # Preferred: grouped by reason. Usually everything fits in one message.
    grouped = _grouped_detail(failed)
    if grouped:
        single = f"{header}\n\n" + "\n\n".join(grouped)
        # The grouped form is measured, never assumed to fit: 200 failures
        # overflow it, and Telegram REJECTS an over-limit message outright
        # (it does not truncate it), so the check is not optional.
        if len(single + log_tail) <= max_chars:
            return [single + log_tail]

    # Fallback: one row per site, split across as many messages as needed.
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for shop in failed:
        line = _shop_detail(shop)
        if current and (
            current_len + len(line) + 1 > max_chars - len(header) - 2
            or len(current) >= max_per_chunk
        ):
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append(current)

    total = len(chunks)
    out: list[str] = []
    for i, lines in enumerate(chunks, start=1):
        title = header + (f"  ({i}/{total})" if total > 1 else "")
        body = "\n".join([title, ""] + lines)
        out.append(body + (log_tail if i == total else ""))
    return out


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
        own_loop: bool = True,
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
        # own_loop=False => a LiveRunPublisher drives this chat and owns the
        # render/tick, so N chats cost one render instead of N.
        self.own_loop = own_loop
        self.is_active = True

        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._last_text = ""
        self._last_call: float | None = None
        self._parse_mode: str | None = "HTML"
        self._broken = False
        self._penalty = 1
        self._seen_errors: set[str] = set()
        self.message_id: int | None = None

    # -- lifecycle --------------------------------------------------------
    async def start(self, *, reset: bool | None = None) -> int:
        """Send the message and (if own_loop) start the refresh task."""
        if reset is None:
            reset = self.own_loop
        if reset:
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
        if self.own_loop:
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
        if self._broken or not self.is_active or self.message_id is None:
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

    async def publish(self, text: str | None = None) -> None:
        """Push the current state to this chat (used by LiveRunPublisher)."""
        await self._flush(pre_rendered=text)

    async def send_followup(self, text: str) -> None:
        """Send one extra message to this chat (the failure report).

        Goes through the same throttle and error handling as an edit, so a 429
        mid-report is honoured instead of flooding the chat.
        """
        if not self.is_active:
            return
        await self._gate()
        try:
            await self.bot.send_message(
                self.chat_id, text, parse_mode=self._parse_mode
            )
            self._last_call = self._clock()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_error(exc, text)

    # -- internals --------------------------------------------------------
    async def _flush(self, pre_rendered: str | None = None) -> None:
        if self._broken or not self.is_active or self.message_id is None:
            return
        text = (
            pre_rendered
            if pre_rendered is not None
            else render(self.progress, self.buffer)
        )
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
# Fan-out publisher: one run, many admins
# --------------------------------------------------------------------------
class LiveRunPublisher:
    """Owns one run's live view and mirrors it into every admin chat.

    The run publishes state; admins are subscribers. Neither the trigger
    (button vs 08:00 cron) nor the admin list is baked into the message
    owner, which is what previously made scheduled runs silent.

    The text is rendered **once** per tick and the same string is pushed to
    every chat, so N admins cost one render and N throttled edits.
    """

    _SUBSCRIBER_GAP = 0.35  # same pacing as broadcast_notification()

    def __init__(
        self,
        bot: Any,
        subscribers: Sequence[int],
        *,
        buffer: LogBuffer | None = None,
        progress: RunProgress | None = None,
        interval: float = _PRIVATE_INTERVAL,
        min_gap: float = _HARD_MIN_GAP,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        loop_sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.bot = bot
        self.buffer = buffer if buffer is not None else get_buffer()
        self.progress = progress if progress is not None else RunProgress()
        self.interval = interval
        self.min_gap = min_gap
        self._clock = clock
        self._sleep = sleep
        # The tick cadence is a separate injectable from the send throttle:
        # tests park the loop so only they drive tick(), while retry_after /
        # min_gap waits inside LiveLogMessage must still go through `sleep`.
        # Folding them into one callable makes a parked loop-sleep swallow
        # error-handling waits too, which hangs the run instead of the test.
        self._loop_sleep = loop_sleep if loop_sleep is not None else sleep

        seen: set[int] = set()
        self.subscribers: dict[int, LiveLogMessage] = {}
        self._order: list[int] = []
        for chat_id in subscribers or ():
            if chat_id in seen:
                continue
            seen.add(chat_id)
            self._order.append(chat_id)
            self.subscribers[chat_id] = LiveLogMessage(
                bot,
                chat_id,
                self.buffer,
                self.progress,
                min_gap=min_gap,
                clock=clock,
                sleep=sleep,
                own_loop=False,
            )

        self._task: asyncio.Task | None = None
        self._last_text = ""
        self._penalty = 1
        self._seen_errors: set[str] = set()

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Send one placeholder per admin, then start the shared tick."""
        self.buffer.clear()
        self.progress.reset()
        for chat_id in self._order:
            sub = self.subscribers[chat_id]
            try:
                await sub.start(reset=False)
            except Exception as exc:
                sub.is_active = False
                self._note(f"subscriber {chat_id} start failed", exc)
            await self._sleep(self._SUBSCRIBER_GAP)
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self._loop_sleep(self.interval * self._penalty)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note("publisher tick failed", exc)

    async def tick(self) -> None:
        """Render once, then push the same text to every healthy chat."""
        text = render(self.progress, self.buffer)
        if text == self._last_text:
            return
        alive = [s for s in self.subscribers.values() if s.is_active]
        if not alive:
            return
        await asyncio.gather(
            *(s.publish(text) for s in alive), return_exceptions=True
        )
        self._last_text = text

    def fail_run(self, error: str) -> None:
        """Make a run-level failure visible in every admin's message."""
        self.progress.run_error = str(error)

    async def stop(self, final_text: str | None = None) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        for chat_id in self._order:
            sub = self.subscribers[chat_id]
            if not sub.is_active:
                continue
            try:
                await sub.stop(final_text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note(f"subscriber {chat_id} final update failed", exc)
        await self._send_failure_report()

    async def _send_failure_report(self) -> None:
        """Post every failed site as follow-up messages.

        One message cannot hold 55 failures and stay readable, so the detail
        that did not fit goes out as separate messages. Rendered once and sent
        to all admins, mirroring how the live message fans out.
        """
        try:
            chunks = render_failure_report(self.progress, self.buffer)
        except Exception as exc:
            self._note("failure report render failed", exc)
            return
        if not chunks:
            return
        self._note_info(f"failure report: {len(chunks)} message(s)")
        for chat_id in self._order:
            sub = self.subscribers[chat_id]
            if not sub.is_active:
                continue
            for chunk in chunks:
                try:
                    await sub.send_followup(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._note(f"failure report to {chat_id} failed", exc)
                    break  # one broken chat must not retry the same chunk
            await self._sleep(self._SUBSCRIBER_GAP)

    def _note_info(self, message: str) -> None:
        logger.info(message)

    def _note(self, context: str, exc: Exception) -> None:
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
