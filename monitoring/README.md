# Competitor Monitor

Telegram bot + scheduler that monitors competitor bridal-shop websites for new
collection pages, analyzes changes with DeepSeek, and logs findings to Google
Sheets.

## What it does

1. Cron at 08:00 Moscow time (or manual "🚀 Начать парсинг" in Telegram)
   triggers a check
2. For each competitor URL in the Google Sheet "Отслеживаемые конкуренты":
   - Sitemap discovery (`/sitemap.xml`, `/robots.txt` → sitemap, link fallback)
   - Diff vs known URLs in `data/tracker.db`
   - For new URLs: cluster + sample → scrape markdown → DeepSeek analysis
3. Sends summary to all admin users via Telegram
4. Logs new collections to Google Sheet "Новинки у конкурентов"

## Local dev

```bash
cd monitoring
uv sync
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN, SPREADSHEET_ID, AI_API_KEY
# credentials.json from Google Cloud service account goes in this dir
uv run main.py
```

## Tests

```bash
uv run pytest tests/
```

## Docker deploy

```bash
cd monitoring
docker build -t competitor-monitor .
docker run -d --name competitor-monitor \
  --restart unless-stopped \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/credentials.json:/app/credentials.json:ro \
  competitor-monitor
```

## Logs

`logs/app.log` is rotated daily by loguru. View with `tail -f logs/app.log`.

## Data

`data/tracker.db` (sqlitedict) — single source of truth for known URLs per
competitor. Safe to delete for a clean reset; the bot will re-initialize on
the next run.
