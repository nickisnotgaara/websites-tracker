import datetime
from dataclasses import dataclass
from typing import List
from loguru import logger
import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.config import settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = "credentials.json"
COMPETITORS_SHEET = "Отслеживаемые конкуренты"


@dataclass
class CompetitorRow:
    """One row of the competitors sheet, including the manual verdict."""

    url: str
    country: str = ""
    city: str = ""
    brand: str = ""
    priority: str = ""
    sheet_mark: bool = False  # column F says "не парятся" — display only

# Google's own clock, used to detect a skewed local clock before blaming the key.
_GOOGLE_TIME_URL = "https://www.googleapis.com/generate_204"


from tenacity import retry, stop_after_attempt, wait_exponential


class GSheetsService:
    def __init__(self):
        self.creds = None
        self.service = None
        self._authenticate()

    def _authenticate(self):
        try:
            self.creds = Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
            self.service = build("sheets", "v4", credentials=self.creds)
            logger.info("Authenticated with Google Sheets successfully.")
        except Exception as e:
            logger.error(f"Failed to authenticate with Google Sheets: {e}")
            raise

    @staticmethod
    def clock_skew_seconds() -> float | None:
        """Signed seconds our clock is off from Google's (None if unknown).

        The JWT is stamped with the *local* clock, so a wrong clock makes
        Google reject a perfectly valid key with a misleading 'Invalid JWT'
        error. Measuring the skew first tells the two failure modes apart.
        """
        try:
            server = requests.head(_GOOGLE_TIME_URL, timeout=10).headers["Date"]
        except Exception:
            return None
        # RFC 7231 date, always GMT
        from email.utils import parsedate_to_datetime

        try:
            google_utc = parsedate_to_datetime(server).timestamp()
        except Exception:
            return None
        return round(datetime.datetime.now(datetime.timezone.utc).timestamp() - google_utc, 1)

    def preflight(self) -> tuple[bool, str]:
        """Actually mint a token and classify the failure instead of guessing.

        ``_authenticate`` only parses the key file, so a revoked key used to
        stay invisible until the first cron run at 08:00.
        """
        skew = self.clock_skew_seconds()
        if skew is not None and abs(skew) > 60:
            logger.warning(
                f"Local clock is {skew:+.0f}s from Google's. Google rejects JWTs "
                f"stamped off-time with 'must be a short-lived token ... reasonable "
                f"timeframe'. Fix the clock before trusting any auth error."
            )

        try:
            self.creds.refresh(Request())
            return True, "Google Sheets auth OK."
        except RefreshError as e:
            detail = str(e)
            if "Invalid JWT Signature" in detail:
                return False, (
                    "Google rejected the key signature. The service account key "
                    f"{SERVICE_ACCOUNT_FILE} was deleted or revoked in GCP — create a "
                    "new key for the service account and replace the file. The key "
                    "file itself parses fine, so no local edit can fix this."
                )
            if "reasonable timeframe" in detail or "short-lived token" in detail:
                measured = f"{skew:+.0f}s" if skew is not None else "unmeasured"
                return False, (
                    f"Google rejected the JWT timestamps (local clock skew {measured}). "
                    "This one self-heals once the system clock is correct."
                )
            return False, f"Token refresh failed: {detail}"
        except Exception as e:
            return False, f"Could not reach Google to verify auth: {e}"

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def get_competitor_rows(self) -> List["CompetitorRow"]:
        """
        Reads the 'Отслеживаемые конкуренты' sheet (columns A-F).

        Columns: Ссылка, Страна, Город, Бренд, Приоритет, парсится ли?
        The header row and any non-http cell is skipped, duplicates are
        collapsed (the sheet currently holds 316 rows -> 306 unique sites),
        and the manual "не парсятся" verdict in column F is carried along as
        `sheet_mark` for the report. The mark is *display only* — marked rows
        are still parsed.
        """
        try:
            sheet = self.service.spreadsheets()
            result = (
                sheet.values()
                .get(
                    spreadsheetId=settings.SPREADSHEET_ID,
                    range=f"'{COMPETITORS_SHEET}'!A:F",
                )
                .execute()
            )

            rows = result.get("values", [])
            out: List["CompetitorRow"] = []
            seen: set[str] = set()
            for row in rows:
                if not row:
                    continue
                url = (row[0] or "").strip()
                # skips the header row and any stray text
                if not url.startswith("http") or url in seen:
                    continue
                seen.add(url)
                mark = (row[5] if len(row) > 5 else "").strip().lower()
                out.append(
                    CompetitorRow(
                        url=url,
                        country=(row[1] if len(row) > 1 else "").strip(),
                        city=(row[2] if len(row) > 2 else "").strip(),
                        brand=(row[3] if len(row) > 3 else "").strip(),
                        priority=(row[4] if len(row) > 4 else "").strip(),
                        sheet_mark="не пар" in mark,
                    )
                )

            logger.info(f"Loaded {len(out)} competitors from Google Sheets.")
            return out

        except Exception as e:
            logger.error(f"Failed to fetch competitors from GSheets: {e}")
            raise  # Retry depends on raising exception

    def get_competitors(self) -> List[str]:
        """Backwards-compatible URL-only view of `get_competitor_rows`."""
        return [row.url for row in self.get_competitor_rows()]

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def log_new_collection(self, url: str, summary: str, new_pages: List[str]):
        """
        Logs a new collection to the 'Новинки у конкурентов' sheet.
        Columns: [Link, Date, Summary, New Pages List]
        """
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Truncate new_pages if too long for a single cell (Google Sheets limit ~50k chars, but let's be safe)
            new_pages_str = ", ".join(new_pages)
            if len(new_pages_str) > 40000:
                new_pages_str = new_pages_str[:40000] + "... (truncated)"

            values = [[url, now, summary, new_pages_str]]

            body = {"values": values}

            self.service.spreadsheets().values().append(
                spreadsheetId=settings.SPREADSHEET_ID,
                range="'Новинки у конкурентов'!A:D",
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()

            logger.info(f"Logged new collection for {url} to GSheets.")
        except Exception as e:
            logger.error(f"Failed to log to GSheets: {e}")
            raise  # Retry depends on raising


gsheets_service = GSheetsService()
