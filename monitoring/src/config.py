from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_ADMIN_IDS: list[int] = []  # Initial admins defined in env

    # Firecrawl
    FIRECRAWL_API_URL: str = "http://localhost:3002"
    FIRECRAWL_API_KEY: str = (
        "fc-YOUR_KEY"  # Placeholder if local doesn't require it, but lib might
    )

    # Ollama / AI
    AI_PROVIDER: str = "deepseek"  # "ollama" or "deepseek"
    AI_API_KEY: str = None

    OLLAMA_BASE_URL: str = "http://localhost:11434/v1"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    AI_BASE_URL: str | None = None  # Override for custom providers

    # Models
    OLLAMA_MODEL: str = "gpt-oss:120b-cloud"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    AI_MODEL_NAME: str | None = None  # If set, overrides provider defaults

    # Storage
    DATA_DIR: Path = Path("data")
    DB_FILENAME: str = "tracker.db"
    SPREADSHEET_ID: str

    # Scheduling
    DEFAULT_CRON_INTERVAL_MINUTES: int = 60

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def db_path(self) -> Path:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        return self.DATA_DIR / self.DB_FILENAME


settings = Settings()
