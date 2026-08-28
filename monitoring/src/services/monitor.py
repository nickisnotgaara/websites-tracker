import asyncio
from typing import List, Set
from urllib.parse import urlparse, urlunparse
from loguru import logger
import re

from src.config import settings
from src.storage import db
from src.services import discovery
from src.services.ai import ai_service
from src.services.gsheets import gsheets_service


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

    async def check_competitor(self, domain_url: str):
        """
        Full check cycle with Smart Batching.
        """
        normalized_domain = normalize_url(domain_url)
        logger.info(f"Starting check for {normalized_domain}")

        # 1. Check History
        known_urls_raw = db.get_known_urls(normalized_domain)
        is_known_site = len(known_urls_raw) > 0
        known_map = {normalize_url(u) for u in known_urls_raw}

        # 2. Map Site (discover_urls has its own retry+timeout; never raises)
        curr_urls_raw = await asyncio.to_thread(
            discovery.discover_urls, domain_url
        )

        if not curr_urls_raw:
            logger.error(
                f"No URLs discovered for {domain_url}. Skipping."
            )
            return

        current_map = {normalize_url(u) for u in curr_urls_raw}

        # 3. Init Phase
        if not is_known_site:
            logger.info(
                f"First time seeing {domain_url}. Initializing database with {len(current_map)} URLs."
            )
            db.update_known_urls(normalized_domain, current_map)
            # Log silent init to GSheets if needed, or skip
            return

        # 4. Update Phase
        new_urls = current_map - known_map

        if not new_urls:
            logger.info(f"No new pages found for {domain_url}")
            return

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
            try:
                content = await asyncio.to_thread(discovery.scrape_markdown, url)
                if content:
                    updates_found.append((url, content))
            except Exception as e:
                logger.error(f"Skipping scrape for {url}: {e}")

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

    async def run_check_cycle(self):
        """
        Main Loop:
        1. Fetch competitors from Google Sheets.
        2. Iterate and check each.
        """
        logger.info("Fetching competitors from Google Sheets...")
        competitors = await asyncio.to_thread(gsheets_service.get_competitors)

        if not competitors:
            logger.warning("No competitors found in Google Sheets!")
            return

        logger.info(f"Found {len(competitors)} competitors to check.")

        for domain in competitors:
            await self.check_competitor(domain)


monitor_service = MonitorService()
