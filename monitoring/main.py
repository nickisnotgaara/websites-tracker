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
    msk_tz = pytz.timezone("Europe/Moscow")

    # Schedule job at 8:00 AM MSK daily
    scheduler.add_job(
        run_parsing_job,
        CronTrigger(hour=8, minute=0, timezone=msk_tz),
        id="daily_check",
        name="Daily Competitor Check",
    )

    scheduler.start()
    logger.info("Scheduler started. Job scheduled for 08:00 MSK daily.")

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
