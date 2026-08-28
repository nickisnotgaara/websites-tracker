from openai import OpenAI
from loguru import logger
from src.config import settings


class AIService:
    def __init__(self):
        base_url = settings.OLLAMA_BASE_URL
        api_key = settings.AI_API_KEY

        if settings.AI_PROVIDER == "deepseek":
            base_url = settings.DEEPSEEK_BASE_URL

        # Allow explicit override
        if settings.AI_BASE_URL:
            base_url = settings.AI_BASE_URL

        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
        )

        # Determine model
        if settings.AI_MODEL_NAME:
            self.model = settings.AI_MODEL_NAME
        elif settings.AI_PROVIDER == "deepseek":
            self.model = settings.DEEPSEEK_MODEL
        else:
            self.model = settings.OLLAMA_MODEL

    def analyze_collection(self, content: str, url: str) -> str | None:
        """
        Analyzes the page content to determine if it's a NEW wedding dress collection.
        Returns a summary if it is, or None if irrelevant.
        """
        prompt = f"""
        You are an expert fashion analyst for a bridal boutique.
        Analyze the content of the following webpage strictly.
        URL: {url}
        
        Task:
        Summarize the content of this webpage in Russian using the context of a bridal/fashion website.
        Describe what is on the page (collections, dresses, text, news).
        
        Output format: A short, engaging summary in Russian.

        Content Snippet (first 8000 chars):
        {content[:8000]}
        """

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful assistant. Summarize the content in Russian.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )

            result = response.choices[0].message.content.strip()
            return result
        except Exception as e:
            logger.error(f"AI analysis failed for {url}: {e}")
            return "Не удалось получить саммари (ошибка AI)."

    def analyze_updates_batch(
        self, updates: list[tuple[str, str]], context_info: list[str] = None
    ) -> str:
        """
        Analyzes multiple new pages together to provide a consolidated summary.
        updates: List of (url, content) tuples.
        context_info: List of cluster stats (e.g. "Collection X: 50 items").
        """
        if not updates and not context_info:
            return "Нет новых данных."

        # Prepare context text with stats first
        context_text = "SUMMARY OF DETECTED UPDATES (Based on URL clustering):\n"
        if context_info:
            for info in context_info:
                context_text += f"- {info}\n"

        context_text += "\nSAMPLE CONTENT FROM NEW PAGES:\n"
        for i, (url, content) in enumerate(updates, 1):
            # Truncate content for token limit safely
            snippet = content[:2000].replace("\n", " ")
            context_text += f"\n--- Sample {i} ---\nURL: {url}\nContent: {snippet}\n"

        prompt = f"""
        You are an expert fashion analyst for a bridal boutique.
        We detected a large update on a competitor's website.
        
        Input Data:
        1. A list of detected "Clusters" (groups of new URLs found).
        2. Content samples from 1-3 representative pages.

        Task:
        Write a SHORT, CONCISE summary in Russian (approx 50-70 words).
        Explain exactly WHAT appeared (e.g., "New Collection 'Spring 2026' was added with 50 dresses").
        Do NOT list every single dress. Focus on the COLLECTIONS or CATEGORIES.
        
        Input Context:
        {context_text}
        
        Output format:
        Start directly with a brief text explaining the update.
        Then bullet points with key collections found.
        DO NOT include a header title like "Update on site".
        
        IMPORTANT Guidelines:
        - Use ONLY HTML tags: <b>, <i>.
        - NO <ul>, <ol>, <li>, Markdown, or HTML entities.
        - Use emojis (e.g. 🔹, ✨) for bullets.
        - Keep it under 100 words total.
        """

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful assistant. Summarize updates in Russian using HTML.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Batch AI analysis failed: {e}")
            return "Не удалось сформировать общий отчет (ошибка AI)."


ai_service = AIService()
