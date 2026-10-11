"""Per-domain health across runs: which sites keep failing, and for how long.

Stored under one new key in the existing sqlitedict database -- no schema
migration, `competitors` / `users` / `app_config` are untouched. The whole
store is written **once per run** rather than once per site: a 306-site run
would otherwise mean 306 SQLite writes.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from loguru import logger
from sqlitedict import SqliteDict

from src.config import settings

HEALTH_KEY = "shop_health"
DEAD_AFTER_RUNS = 3  # consecutive failed runs before a site is flagged


class ShopHealthStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or settings.db_path

    # -- storage ---------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            with SqliteDict(self.db_path) as db:
                return dict(db.get(HEALTH_KEY, {}) or {})
        except Exception as e:
            logger.warning(f"Could not read {HEALTH_KEY}: {e}")
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        try:
            with SqliteDict(self.db_path, autocommit=True) as db:
                db[HEALTH_KEY] = data
        except Exception as e:
            logger.warning(f"Could not persist {HEALTH_KEY}: {e}")

    # -- api -------------------------------------------------------------
    def record_run(
        self,
        results: Iterable[dict],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Fold one run's per-site outcomes into the streaks.

        ``results`` items: {"url", "failed", "code", "detail"}.
        Returns the updated store.
        """
        data = self._load()
        stamp = now if now is not None else time.time()
        for item in results:
            url = item.get("url")
            if not url:
                continue
            rec = data.get(url) or {
                "fails_streak": 0,
                "last_ok_at": None,
                "last_fail_at": None,
                "last_code": None,
                "last_detail": None,
                "runs": 0,
            }
            rec["runs"] = rec.get("runs", 0) + 1
            if item.get("failed"):
                rec["fails_streak"] = rec.get("fails_streak", 0) + 1
                rec["last_fail_at"] = stamp
                rec["last_code"] = item.get("code")
                rec["last_detail"] = item.get("detail")
            else:
                rec["fails_streak"] = 0
                rec["last_ok_at"] = stamp
                rec["last_code"] = None
                rec["last_detail"] = None
            data[url] = rec
        self._save(data)
        return data

    def dead(self, threshold: int = DEAD_AFTER_RUNS) -> list[dict[str, Any]]:
        """Sites that failed ``threshold`` runs in a row."""
        return [
            {"url": url, **rec}
            for url, rec in self._load().items()
            if rec.get("fails_streak", 0) >= threshold
        ]

    def streaks(self) -> dict[str, int]:
        return {url: rec.get("fails_streak", 0) for url, rec in self._load().items()}