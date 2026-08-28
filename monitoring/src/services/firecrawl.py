from firecrawl import FirecrawlApp
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential
from src.config import settings


class FirecrawlService:
    def __init__(self):
        self.app = FirecrawlApp(
            api_key=settings.FIRECRAWL_API_KEY, api_url=settings.FIRECRAWL_API_URL
        )

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def map_site(self, url: str) -> list[str]:
        """
        Maps a website to discover all its URLs.
        """
        try:
            logger.info(f"Mapping site: {url}")
            # v2 API uses .map(url)
            map_result = self.app.map(url)

            # Ensure we extract simple URLs
            urls = []
            if isinstance(map_result, dict):
                # Check for 'links' or 'data'
                if "links" in map_result:
                    urls = map_result["links"]
                elif "data" in map_result:
                    data = map_result["data"]
                    if isinstance(data, list):
                        urls = [
                            item.get("url")
                            for item in data
                            if isinstance(item, dict) and "url" in item
                        ]
            elif hasattr(map_result, "links"):
                urls = []
                for item in map_result.links:
                    if isinstance(item, str):
                        urls.append(item)
                    elif hasattr(item, "url"):
                        urls.append(item.url)
                    elif isinstance(item, dict) and "url" in item:
                        urls.append(item["url"])
            elif hasattr(map_result, "data"):
                # Assuming data might be a list of objects or dicts
                urls = []
                data = map_result.data
                if isinstance(data, list):
                    for item in map_result.data:
                        if isinstance(item, str):
                            urls.append(item)
                        elif hasattr(item, "url"):
                            urls.append(item.url)
                        elif isinstance(item, dict) and "url" in item:
                            urls.append(item["url"])

            logger.info(f"Found {len(urls)} URLs for {url}")
            return urls
        except Exception as e:
            logger.error(f"Failed to map {url}: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def scrape_url(self, url: str) -> str:
        """
        Scrapes a single page and returns its markdown content.
        """
        try:
            logger.info(f"Scraping URL: {url}")
            # v2 API uses .scrape(url, formats=['markdown'])
            scrape_result = self.app.scrape(url, formats=["markdown"])

            logger.debug(f"Raw Scrape Result Type: {type(scrape_result)}")
            logger.debug(f"Raw Scrape Result: {scrape_result}")

            # Handling response
            content = ""
            if isinstance(scrape_result, dict):
                # Direct dict or nested in data
                data = scrape_result.get("data", scrape_result)
                content = data.get("markdown", "")
            elif hasattr(scrape_result, "markdown"):
                content = scrape_result.markdown

            if not content:
                logger.warning(f"No content extracted for {url}")

            return content
        except Exception as e:
            logger.error(f"Failed to scrape {url}: {e}")
            raise


firecrawl_service = FirecrawlService()
