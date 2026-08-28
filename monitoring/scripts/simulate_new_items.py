import sys
import random
import time
from pathlib import Path
from sqlitedict import SqliteDict

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings


def main():
    print("🧪 Simulating 'New Items' by deleting history from DB...")
    print(f"DB Path: {settings.db_path}")

    try:
        with SqliteDict(settings.db_path, autocommit=True) as db:
            competitors = db.get("competitors", {})
            if not competitors:
                print("❌ No competitors found in DB.")
                return

            all_domains = list(competitors.keys())

            # Select up to 15 random domains
            num_sites = min(len(all_domains), 15)
            target_domains = random.sample(all_domains, num_sites)

            print(f"🎯 Selected {num_sites} sites for modification.")

            for domain in target_domains:
                urls = competitors[domain]  # specific list of URLs

                # We need at least 20 items to safely remove 10-15
                if len(urls) < 20:
                    print(f"⚠️  Skipping {domain}: Too few URLs ({len(urls)})")
                    continue

                # Try to pick "interesting" URLs first (deep links), but random is fine
                num_to_delete = random.randint(10, 15)

                # Shuffle and pick victims
                random.shuffle(urls)
                removed = urls[:num_to_delete]
                remaining = urls[num_to_delete:]

                # Update the dictionary
                competitors[domain] = remaining

                print(
                    f"✅ {domain}: Removed {num_to_delete} URLs (Simulating them as 'New' next run)."
                )
                # print(f"   Deleted samples: {removed[:2]}...")

            # Commit changes to DB
            db["competitors"] = competitors
            print("\n💾 Database updated successfully.")
            print(
                "🚀 NOW: Run the bot or '/test' command. It should re-discover these deleted URLs as NEW and trigger notifications."
            )

    except Exception as e:
        print(f"❌ Error modifying DB: {e}")
        print("Try stopping the bot (Ctrl+C) if it's running, to release DB lock.")


if __name__ == "__main__":
    main()
