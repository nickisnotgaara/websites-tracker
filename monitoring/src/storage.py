from pathlib import Path
from typing import Set, List, Dict, Any, Optional
from sqlitedict import SqliteDict
from loguru import logger
from src.config import settings


class Storage:
    def __init__(self, db_path: Path = settings.db_path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize tables (keys) if they don't exist."""
        with SqliteDict(self.db_path, autocommit=True) as db:
            if "competitors" not in db:
                db["competitors"] = {}  # {domain: set(urls)}

            # Users Migration & Init
            if "users" not in db:
                # Initialize with env vars if empty
                initial_users = [
                    {"id": uid, "name": f"Admin {uid}"}
                    for uid in settings.TELEGRAM_ADMIN_IDS
                ]
                db["users"] = initial_users
            else:
                # MIGRATION: Check if it's a list of ints, convert to dicts
                users = db["users"]
                if users and isinstance(users[0], int):
                    logger.info("Migrating users from List[int] to List[Dict]...")
                    new_users = [{"id": uid, "name": f"User {uid}"} for uid in users]
                    db["users"] = new_users
                    logger.info("Migration complete.")

            if "app_config" not in db:
                db["app_config"] = {
                    "cron_minutes": settings.DEFAULT_CRON_INTERVAL_MINUTES
                }

    # --- Competitor Tracking ---

    def get_known_urls(self, domain: str) -> Set[str]:
        with SqliteDict(self.db_path) as db:
            competitors = db.get("competitors", {})
            return set(competitors.get(domain, []))

    def update_known_urls(self, domain: str, new_urls: Set[str]):
        """Add new_urls to the existing set for the domain."""
        with SqliteDict(self.db_path, autocommit=True) as db:
            competitors = db.get("competitors", {})
            current = set(competitors.get(domain, []))
            updated = current | new_urls
            competitors[domain] = list(
                updated
            )  # Store as list as JSON/Pickle compatibility
            db["competitors"] = competitors
            logger.debug(f"Updated known URLs for {domain}. Total: {len(updated)}")

    def get_all_competitors(self) -> List[str]:
        with SqliteDict(self.db_path) as db:
            return list(db.get("competitors", {}).keys())

    def add_competitor(self, domain: str):
        with SqliteDict(self.db_path, autocommit=True) as db:
            competitors = db.get("competitors", {})
            if domain not in competitors:
                competitors[domain] = []
                db["competitors"] = competitors
                logger.info(f"Added new competitor: {domain}")

    # --- User Management ---

    def get_users(self) -> List[int]:
        """Returns list of User IDs only (for broadcast compatibility)."""
        with SqliteDict(self.db_path) as db:
            users = db.get("users", [])
            # Handle mixed types just in case, or assume migration worked
            return [u["id"] if isinstance(u, dict) else u for u in users]

    def get_users_full(self) -> List[Dict[str, Any]]:
        """Returns list of user dicts: [{'id': 123, 'name': 'Bob'}]"""
        with SqliteDict(self.db_path) as db:
            return db.get("users", [])

    def add_user(self, user_id: int, name: str = None) -> bool:
        with SqliteDict(self.db_path, autocommit=True) as db:
            users = db.get("users", [])
            # Check if exists
            for u in users:
                u_id = u["id"] if isinstance(u, dict) else u
                if u_id == user_id:
                    return False

            new_user = {"id": user_id, "name": name or f"User {user_id}"}
            users.append(new_user)
            db["users"] = users
            logger.info(f"Added user: {new_user}")
            return True

    def remove_user(self, user_id: int) -> bool:
        with SqliteDict(self.db_path, autocommit=True) as db:
            users = db.get("users", [])
            original_len = len(users)

            # Filter out
            # Handle both legacy ints and new dicts safely
            new_users = [
                u for u in users if (u["id"] if isinstance(u, dict) else u) != user_id
            ]

            if len(new_users) == original_len:
                return False

            db["users"] = new_users
            logger.info(f"Removed user: {user_id}")
            return True

    # --- App Config ---

    def get_cron_interval(self) -> int:
        with SqliteDict(self.db_path) as db:
            config = db.get("app_config", {})
            return config.get("cron_minutes", settings.DEFAULT_CRON_INTERVAL_MINUTES)

    def set_cron_interval(self, minutes: int):
        with SqliteDict(self.db_path, autocommit=True) as db:
            config = db.get("app_config", {})
            config["cron_minutes"] = minutes
            db["app_config"] = config
            logger.info(f"Updated cron interval to {minutes} minutes")


db = Storage()
