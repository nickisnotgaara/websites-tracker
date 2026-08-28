import datetime
from typing import List
from loguru import logger
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.config import settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = "credentials.json"


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

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def get_competitors(self) -> List[str]:
        """
        Reads URLs from the 'Отслеживаемые конкуренты' sheet (Column A).
        """
        try:
            sheet = self.service.spreadsheets()
            result = (
                sheet.values()
                .get(
                    spreadsheetId=settings.SPREADSHEET_ID,
                    range="'Отслеживаемые конкуренты'!A:A",
                )
                .execute()
            )

            rows = result.get("values", [])
            urls = []
            for row in rows:
                if row and row[0].strip().startswith("http"):
                    urls.append(row[0].strip())

            # Remove duplicates and empty
            return list(set(urls))
        except Exception as e:
            logger.error(f"Failed to fetch competitors from GSheets: {e}")
            raise  # Retry depends on raising exception

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
