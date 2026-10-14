import asyncio
import sys
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from src.config import settings
from src.storage import db
from src.services.bot.livelog import get_buffer, install_sink
from src.services.bot.main import dp, bot, run_parsing_job, run_startup_job


_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_LABELS = {
    "mon": "пн",
    "tue": "вт",
    "wed": "ср",
    "thu": "чт",
    "fri": "пт",
    "sat": "сб",
    "sun": "вс",
}


def _build_trigger(tz) -> tuple[CronTrigger, str]:
    """Cron trigger from settings, plus a human-readable description.

    Validating the day names here turns a silent APScheduler error at boot
    into a message that says which value is wrong.
    """
    raw = (settings.SCHEDULE_DAYS or "*").strip().lower()
    if raw in ("*", "all", "daily", ""):
        days = "*"
        day_desc = "ежедневно"
    else:
        requested = [d.strip() for d in raw.split(",") if d.strip()]
        unknown = [d for d in requested if d not in _DAY_NAMES]
        if unknown:
            raise ValueError(
                f"SCHEDULE_DAYS has unknown day(s): {unknown}. "
                f"Use any of {', '.join(_DAY_NAMES)}, e.g. 'mon,thu'."
            )
        days = ",".join(requested)
        labels = [_DAY_LABELS[d] for d in requested]
        day_desc = ", ".join(labels)

    trigger = CronTrigger(
        hour=settings.SCHEDULE_HOUR,
        minute=settings.SCHEDULE_MINUTE,
        day_of_week=days,
        timezone=tz,
    )
    return trigger, f"{day_desc} в {settings.SCHEDULE_HOUR:02d}:{settings.SCHEDULE_MINUTE:02d}"


async def main():
    logger.info("Starting Competitor Tracker...")

    # Verify we have admins
    users = db.get_users()
    if not users:
        logger.warning("No users/admins found! Add yourself via .env or DB.")
    else:
        logger.info(f"Loaded {len(users)} authorized users.")

    # Initialize Scheduler
    scheduler = AsyncIOScheduler()
    msk_tz = pytz.timezone(settings.SCHEDULE_TZ)

    trigger, schedule_desc = _build_trigger(msk_tz)
    scheduler.add_job(
        run_parsing_job,
        trigger,
        id="scheduled_check",
        name="Scheduled Competitor Check",
    )

    scheduler.start()
    logger.info(f"Scheduler started. Next run: {schedule_desc} ({settings.SCHEDULE_TZ}).")

    # Start Startup Init (Background)
    # Disabled by user request: Only start on Manual Trigger or Cron
    # logger.info("Scheduling startup check...")
    # asyncio.create_task(run_startup_job())

    # Start Bot Polling
    # We await this as it blocks (keeps the event loop running).
    # A transient network reset must not kill the whole process -- that would
    # take the 08:00 scheduler down with it. Retry with capped backoff instead.
    backoff = 5.0
    while True:
        try:
            await dp.start_polling(bot)
            break  # polling stopped cleanly (shutdown requested)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Bot polling failed: {e} — retry in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


if __name__ == "__main__":
    # Setup loguru
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add("logs/app.log", rotation="1 day", level="DEBUG")

    # Mirror the terminal stream into a buffer the Telegram live log renders.
    # No-op when TELEGRAM_LIVE_LOG=false.
    install_sink(
        get_buffer(),
        level=settings.TELEGRAM_LIVE_LOG_LEVEL,
        enabled=settings.TELEGRAM_LIVE_LOG,
        deny=settings.TELEGRAM_LIVE_LOG_DENY,
    )

    try:
        # Check critical config early
        if not settings.SPREADSHEET_ID:
            logger.error("SPREADSHEET_ID is missing in config/env!")
            sys.exit(1)

        # Prove Google auth really works before the 08:00 cron depends on it.
        # _authenticate() only parses the key file, so a revoked key or a wrong
        # system clock used to stay invisible until the first scheduled run.
        try:
            from src.services.gsheets import gsheets_service

            ok, detail = gsheets_service.preflight()
            if ok:
                logger.info(detail)
            else:
                logger.error(f"Google Sheets preflight FAILED: {detail}")
        except Exception as e:
            logger.error(f"Google Sheets preflight could not run: {e}")

        # Windows selector loop policy fix
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
    except Exception as e:
        logger.exception(f"CRITICAL STARTUP ERROR: {e}")
        # Explicit print ensuring user sees it even if logs fail
        print(f"CRITICAL ERROR: {e}", file=sys.stderr)
        sys.exit(1)
