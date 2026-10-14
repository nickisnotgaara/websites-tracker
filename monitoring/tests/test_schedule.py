"""Tests for the run schedule and the "no new items" vs "error" wording.

Two operator complaints drove this:
  * "twice a week (Mon+Thu) does not work" -- the schedule was hardcoded to
    08:00 daily in main.py, so nothing in .env could change it.
  * "a site with no new items" and "a site we could not check" read the same,
    so the report now states both separately.
"""
import datetime
import sys
import types

import pytest
import pytz

from src.services.bot.livelog import (
    LogBuffer,
    RunProgress,
    render,
    render_failure_report,
)


# --- schedule ----------------------------------------------------------


@pytest.fixture()
def scheduler_module():
    """main.py with Google Sheets stubbed (imports authenticate at import)."""
    if "src.services.gsheets" not in sys.modules:
        stub = types.ModuleType("src.services.gsheets")

        class _Svc:
            def get_competitor_rows(self):
                return []

            def preflight(self):
                return True, "stub"

        stub.gsheets_service = _Svc()
        sys.modules["src.services.gsheets"] = stub

    if "src.services.bot.main" not in sys.modules:
        stub_bot = types.ModuleType("src.services.bot.main")
        stub_bot.dp = object()
        stub_bot.bot = object()
        stub_bot.run_parsing_job = lambda *a, **k: None
        stub_bot.run_startup_job = lambda *a, **k: None
        sys.modules["src.services.bot.main"] = stub_bot

    import main as scheduler_module

    return scheduler_module


TZ = pytz.timezone("Europe/Moscow")
REF = datetime.datetime(2026, 10, 12, 0, 0, tzinfo=TZ)


def _fire_times(module, days, hour=8, minute=0, count=4):
    from src.config import settings

    settings.SCHEDULE_DAYS = days
    settings.SCHEDULE_HOUR = hour
    settings.SCHEDULE_MINUTE = minute
    trigger, desc = module._build_trigger(TZ)
    prev, out = None, []
    for _ in range(count):
        prev = trigger.get_next_fire_time(prev, prev or REF)
        out.append(prev.strftime("%a %H:%M"))
    return out, desc


def test_schedule_can_run_twice_a_week(scheduler_module):
    """The exact request: Monday and Thursday."""
    days, desc = _fire_times(scheduler_module, "mon,thu")
    assert days == ["Mon 08:00", "Thu 08:00", "Mon 08:00", "Thu 08:00"]
    assert "пн" in desc and "чт" in desc


def test_schedule_defaults_to_daily(scheduler_module):
    days, _ = _fire_times(scheduler_module, "*", count=3)
    assert days == ["Mon 08:00", "Tue 08:00", "Wed 08:00"]


def test_schedule_supports_single_days_and_custom_times(scheduler_module):
    days, _ = _fire_times(scheduler_module, "mon", hour=9, minute=30, count=2)
    assert days == ["Mon 09:30", "Mon 09:30"]
    days, _ = _fire_times(scheduler_module, "mon, thu , sat", count=3)
    assert days == ["Mon 08:00", "Thu 08:00", "Sat 08:00"]


def test_invalid_day_name_is_rejected_with_a_useful_message(scheduler_module):
    with pytest.raises(ValueError) as excinfo:
        _fire_times(scheduler_module, "monday,xyz")
    message = str(excinfo.value)
    assert "monday" in message and "xyz" in message
    assert "mon, tue" in message  # tells the operator what IS valid


def test_schedule_settings_exist_in_config():
    """Guard against the settings being dropped again.

    The old DEFAULT_CRON_INTERVAL_MINUTES looked configurable but the
    scheduler never read it, which is why 'I tried different values and nothing
    happened' was literally true.
    """
    from src.config import Settings

    fields = Settings.model_fields
    assert "SCHEDULE_DAYS" in fields
    assert "SCHEDULE_HOUR" in fields
    assert "SCHEDULE_MINUTE" in fields
    assert "SCHEDULE_TZ" in fields


# --- "no new items" is not an error -----------------------------------


def _run():
    buffer = LogBuffer(maxlen=200)
    progress = RunProgress()
    progress.set_total(4)
    progress.begin("https://quiet-shop.example.com")
    progress.finish("https://quiet-shop.example.com", "ok", 0)  # reachable, empty
    progress.begin("https://fresh-shop.example.com")
    progress.finish("https://fresh-shop.example.com", "ok", 7)
    progress.begin("https://blocked-shop.example.com")
    progress.finish(
        "https://blocked-shop.example.com",
        "failed",
        error="403 — блокируют бота",
        http_status=403,
    )
    progress.begin("https://dead-shop.example.com")
    progress.finish(
        "https://dead-shop.example.com", "failed", error="домен не резолвится"
    )
    progress.finish_run()
    return progress, buffer


def test_reachable_shop_with_no_items_is_not_worded_like_a_failure():
    progress, buffer = _run()
    text = render(progress, buffer)

    line = next(l for l in text.splitlines() if "quiet-shop" in l)
    assert line.startswith("✅")
    assert "новых нет" in line
    # The old wording "без новых" sat next to real errors with nothing to tell
    # them apart; the line now states the site *was checked*.
    assert "проверен" in line
    assert "403" not in line and "не резолвится" not in line


def test_failed_sites_keep_their_reason_and_are_counted_apart():
    progress, buffer = _run()
    text = render(progress, buffer)

    assert "blocked-shop.example.com — 403 — блокируют бота" in text
    assert "dead-shop.example.com — домен не резолвится" in text
    assert "✅ 2 · ❌ 2" in text


def test_header_says_so_when_a_clean_run_found_nothing_new():
    """A healthy run with zero new items must say so, not look broken."""
    buffer = LogBuffer(maxlen=200)
    progress = RunProgress()
    progress.set_total(2)
    for url in ("https://quiet-a.example.com", "https://quiet-b.example.com"):
        progress.begin(url)
        progress.finish(url, "ok", 0)
    progress.finish_run()

    text = render(progress, buffer)
    assert "✅ 2 · ❌ 0 · 🆕 нет новых" in text
    assert "🆕 0" not in text


def test_header_keeps_the_new_items_count_when_there_are_some():
    progress, buffer = _run()
    text = render(progress, buffer)
    assert "🆕 7" in text
    assert "нет новых" not in text


def test_failure_report_separates_checked_from_failed():
    progress, buffer = _run()
    text = render_failure_report(progress, buffer)[0]

    assert "Проверено успешно: 2" in text
    assert "без новинок: 1" in text
    assert "с новинками: 1" in text
    # ...and the failure list below is still only failures.
    failures_block = text.split("Проверено успешно: 2", 1)[1]
    assert "quiet-shop" not in failures_block
    assert "fresh-shop" not in failures_block
    assert "blocked-shop.example.com" in failures_block
    assert "dead-shop.example.com" in failures_block