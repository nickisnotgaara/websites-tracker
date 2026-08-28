import asyncio
import sys
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from src.config import settings
from src.storage import db
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
    # We await this as it blocks (keeps the event loop running)
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot polling failed: {e}")


if __name__ == "__main__":
    # Setup loguru
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add("logs/app.log", rotation="1 day", level="DEBUG")

    try:
        # Check critical config early
        if not settings.SPREADSHEET_ID:
            logger.error("SPREADSHEET_ID is missing in config/env!")
            sys.exit(1)

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
