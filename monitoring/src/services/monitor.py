import asyncio
from typing import TYPE_CHECKING, List, Optional, Set
from urllib.parse import urlparse, urlunparse
from loguru import logger
import re

from src.config import settings
from src.storage import db
from src.services import discovery
from src.services.ai import ai_service
from src.services.gsheets import gsheets_service
from src.services.health import DEAD_AFTER_RUNS, ShopHealthStore

if TYPE_CHECKING:
    from src.services.bot.livelog import RunProgress


def clean_html(text: str) -> str:
    """Removes HTML tags from text."""
    clean = re.compile("<.*?>")
    return re.sub(clean, "", text)


def normalize_url(url: str) -> str:
    """
    Normalizes a URL to ensure consistent comparison.
    - Lowercase scheme/netloc
    - Strip trailing slash
    """
    try:
        parsed = urlparse(url)
        # Remove fragments
        clean_path = parsed.path.rstrip("/")
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=clean_path,
            fragment="",
        )
        return urlunparse(normalized)
    except Exception:
        return url.rstrip("/")


def extract_slug(url: str) -> str:
    """Extracts the last non-empty segment of the URL."""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if not path:
            return ""
        return path.split("/")[-1]
    except:
        return ""


def deduplicate_urls(urls: Set[str]) -> Set[str]:
    """
    Groups URLs by slug and picks the best candidate (canonical).
    Strategy:
    1. Group by slug (e.g. 'style-123').
    2. Prefer '/en/' or root domain.
    3. Fallback: Shortest URL.
    """
    slug_map = {}
    for url in urls:
        slug = extract_slug(url)
        if not slug:
            continue

        if slug not in slug_map:
            slug_map[slug] = []
        slug_map[slug].append(url)

    unique_urls = set()
    for slug, candidates in slug_map.items():
        # Sort candidates: prefer 'en', then shortest length
        candidates.sort(key=lambda u: (0 if "/en/" in u else 1, len(u)))
        unique_urls.add(candidates[0])

    return unique_urls


def cluster_and_sample(urls: Set[str]) -> tuple[List[str], List[str]]:
    """
    Groups URLs by parent directory (Collection) and selects samples.
    Returns:
    - urls_to_scrape: Max ~5 representative URLs.
    - context_info: List of strings describing clusters (e.g. 'Collection Spring-2026: 45 items')
    """
    clusters = {}
    for url in urls:
        try:
            parsed = urlparse(url)
            path = parsed.path.rstrip("/")
            parent = path.rsplit("/", 1)[0]  # Get parent directory

            if parent not in clusters:
                clusters[parent] = []
            clusters[parent].append(url)
        except:
            continue

    # Sort clusters by size (biggest first)
    sorted_clusters = sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True)

    urls_to_scrape = []
    context_info = []

    # Limit to top 5 clusters to avoid huge tokens
    for parent, items in sorted_clusters[:5]:
        context_info.append(f"Cluster '{parent}': {len(items)} new items")
        # Take just 1 sample from each cluster
        urls_to_scrape.append(items[0])

    return urls_to_scrape, context_info


class MonitorService:
    def __init__(self, notification_callback=None):
        self.notification_callback = notification_callback

    async def check_competitor(self, domain_url: str) -> dict:
        """
        Full check cycle with Smart Batching.

        Returns a status dict for the caller (and the live Telegram log):
        {"status": "ok" | "failed" | "skipped", "new_urls": int, "error": str | None}
        """
        normalized_domain = normalize_url(domain_url)
        logger.info(f"Starting check for {normalized_domain}")

        # 1. Check History
        known_urls_raw = db.get_known_urls(normalized_domain)
        is_known_site = len(known_urls_raw) > 0
        known_map = {normalize_url(u) for u in known_urls_raw}

        # 2. Map Site (discover_urls_detailed has its own retry+timeout; never raises).
        # Outer 1-retry on empty result catches "200 with empty body" (deployment in progress, etc.)
        discovery_result = await asyncio.to_thread(
            discovery.discover_urls_detailed, domain_url
        )
        if not discovery_result.urls:
            logger.warning(
                f"No URLs discovered for {domain_url} on first attempt; retrying after 5s..."
            )
            await asyncio.sleep(5)
            discovery_result = await asyncio.to_thread(
                discovery.discover_urls_detailed, domain_url
            )

        if not discovery_result.urls:
            # Say *why*, not just that it failed: the taxonomy detail is what
            # makes "домен мёртв" distinguishable from "нас блокируют".
            logger.error(
                f"No URLs discovered for {domain_url} after retry "
                f"[{discovery_result.code}]: {discovery_result.detail}"
            )
            return {
                "status": "failed",
                "new_urls": 0,
                "error": discovery_result.detail
                or "0 URLs discovered after 2 attempts",
                "fail_code": discovery_result.code,
                "http_status": discovery_result.http_status,
                "truncated": False,
            }

        current_map = {normalize_url(u) for u in discovery_result.urls}
        truncated = discovery_result.truncated

        # 3. Init Phase
        if not is_known_site:
            logger.info(
                f"First time seeing {domain_url}. Initializing database with {len(current_map)} URLs."
            )
            db.update_known_urls(normalized_domain, current_map)
            # Log silent init to GSheets if needed, or skip
            return {
                "status": "skipped",
                "new_urls": 0,
                "error": None,
                "fail_code": None,
                "http_status": None,
                "truncated": truncated,
            }

        # 4. Update Phase
        new_urls = current_map - known_map

        if not new_urls:
            logger.info(f"No new pages found for {domain_url}")
            return {
                "status": "ok",
                "new_urls": 0,
                "error": None,
                "fail_code": None,
                "http_status": None,
                "truncated": truncated,
            }

        logger.info(f"Found {len(new_urls)} raw new URLs for {domain_url}")

        # --- SMART BATCHING ---

        # A. Deduplicate (Filter Multilingual)
        unique_urls = deduplicate_urls(new_urls)
        logger.info(f"Filtered to {len(unique_urls)} unique canonical items.")

        # B. Cluster & Sample
        urls_to_scrape, context_info = cluster_and_sample(unique_urls)
        logger.info(f"Sampling {len(urls_to_scrape)} pages for analysis.")
        logger.info(f"Context: {context_info}")

        # C. Scrape Samples
        updates_found = []  # List[(url, content)]

        for url in urls_to_scrape:
            # Basic filter for non-content files
            if any(
                ext in url for ext in [".jpg", ".png", ".css", ".js", ".xml", ".pdf"]
            ):
                continue

            logger.info(f"Scraping sample page: {url}")
            content = await asyncio.to_thread(discovery.scrape_markdown, url)
            if content:
                updates_found.append((url, content))

        # All caught URLs (even unsampled) are saved to DB to prevent re-detection
        db_save_set = new_urls  # We save ALL raw new URLs, not just unique ones

        if updates_found:
            # 1. Batch AI Analysis (Pass context stats)
            summary = await asyncio.to_thread(
                ai_service.analyze_updates_batch,
                updates_found,
                context_info=context_info,
            )

            # 2. Formatted Notification Text
            # Show summary + cluster stats + sample links
            links_list = "\n".join([f"• {u}" for u, _ in updates_found])

            header = f"🆕 <b>Обновление на сайте</b> {domain_url}"
            msg_text = (
                f"{header}\n\n{summary}\n\n📊 <b>Детали обновлений:</b>\n"
                + "\n".join([f"🔹 {c}" for c in context_info])
                + f"\n\n🔗 <b>Примеры страниц:</b>\n{links_list}"
            )

            # 3. Notify
            if self.notification_callback:
                await self.notification_callback(msg_text)

            # 4. Log to GSheets
            new_links_only = [u for u in unique_urls]  # Log canonicals
            clean_summary = clean_html(summary)
            await asyncio.to_thread(
                gsheets_service.log_new_collection,
                domain_url,
                clean_summary,
                new_links_only,
            )

            logger.success(f"Processed batch update for {domain_url}")

        # Update Storage (Save ALL detected raw URLs)
        if db_save_set:
            db.update_known_urls(normalized_domain, db_save_set)

        return {
            "status": "ok",
            "new_urls": len(new_urls),
            "error": None,
            "fail_code": None,
            "http_status": None,
            "truncated": truncated,
        }

    async def run_check_cycle(self, progress: Optional["RunProgress"] = None):
        """
        Main Loop:
        1. Fetch competitors from Google Sheets.
        2. Iterate and check each.

        A shop that raises is logged, reported as failed, and the cycle
        continues -- one broken competitor must not kill the whole run.
        """
        logger.info("Fetching competitors from Google Sheets...")
        try:
            competitors = await asyncio.to_thread(gsheets_service.get_competitor_rows)
        except Exception as e:
            # Recorded on the progress so the live message explains itself,
            # then re-raised so the broadcast path still reports the failure.
            logger.exception("Failed to fetch competitors from Google Sheets")
            if progress is not None:
                progress.run_error = f"Google Sheets: {e}"
                progress.finish_run()
            raise

        if not competitors:
            logger.warning("No competitors found in Google Sheets!")
            if progress is not None:
                progress.run_error = "В таблице нет ни одного сайта"
                progress.finish_run()
            return

        logger.info(f"Found {len(competitors)} competitors to check.")

        if progress is not None:
            progress.set_total(len(competitors))

        outcomes: list[dict] = []
        for row in competitors:
            if progress is not None:
                progress.begin(row.url, sheet_mark=row.sheet_mark)
            try:
                result = await self.check_competitor(row.url)
            except Exception as e:
                logger.exception(f"Check failed for {row.url}: {e}")
                detail = f"{type(e).__name__}: {e}"
                outcomes.append(
                    {"url": row.url, "failed": True, "code": "exception", "detail": detail}
                )
                if progress is not None:
                    progress.finish(
                        row.url, "failed", 0, detail, fail_code="exception"
                    )
                continue

            failed = result.get("status") == "failed"
            outcomes.append(
                {
                    "url": row.url,
                    "failed": failed,
                    "code": result.get("fail_code"),
                    "detail": result.get("error"),
                }
            )
            if progress is not None:
                progress.finish(
                    row.url,
                    result.get("status", "ok"),
                    result.get("new_urls", 0),
                    result.get("error"),
                    fail_code=result.get("fail_code"),
                    http_status=result.get("http_status"),
                    truncated=result.get("truncated", False),
                )

        self._record_health(outcomes, progress)

        if progress is not None:
            progress.finish_run()

    @staticmethod
    def _record_health(outcomes: list[dict], progress) -> None:
        """Persist streaks once per run and surface the dead-site shortlist."""
        try:
            store = ShopHealthStore()
            store.record_run(outcomes)
            if progress is None:
                return
            by_url = {s.url: s for s in progress.shops}
            dead = []
            for entry in store.dead(DEAD_AFTER_RUNS):
                shop = by_url.get(entry["url"])
                if shop is None:
                    continue
                shop.fails_streak = entry.get("fails_streak", 0)
                shop.last_ok_at = entry.get("last_ok_at")
                dead.append(shop)
            progress.dead = dead
        except Exception as e:
            logger.warning(f"Health store update failed: {e}")


monitor_service = MonitorService()
