"""Upwork parser — public RSS feeds.

Upwork exposes per-category RSS feeds without authentication:
  https://www.upwork.com/ab/feed/jobs/rss?q=<query>&sort=recency&paging=0;10

We query several high-demand categories that map to the bot's category list.
Each feed is fetched in parallel (via asyncio.gather in safe_parse's caller).

Notes:
- Upwork titles are in English; description is HTML-escaped.
- Budget may appear as "Fixed-Price: $500" or "Hourly: $15.00-$20.00/hr".
- The external_id is the Upwork job key from the <link> URL.
- We normalize the price string to be consistent with other parsers.

Privacy: Upwork RSS is publicly accessible — no credentials required.
Anti-bot: RSS feeds are served without Cloudflare challenge as of mid-2026.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from bot.constants import EXT_ID_FALLBACK_LIMIT, ORDER_DESCRIPTION_STORED
from bot.parsers.base import BaseParser, ParsedOrder
from bot.parsers.filters import filter_orders

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Upwork job URLs: /jobs/<slug>_~<job_key>/
_JOB_KEY_RE = re.compile(r"~([0-9a-f]+)")
# Budget line in description: "Budget: $500" or "Hourly Range: $15-$20"
_BUDGET_RE = re.compile(
    r"(?:Budget|Hourly\s+Range|Fixed[\s-]Price)\s*:\s*([^\n<]+)",
    re.IGNORECASE,
)

# Map bot categories → Upwork search queries (English).
# Each query fetches up to 10 items; duplicates across queries are deduped
# by external_id in save_orders.
_CATEGORY_QUERIES: dict[str, str] = {
    "Программирование":       "python django fastapi",
    "Веб-разработка":          "web development react vue",
    "Мобильная разработка":    "mobile app android ios flutter",
    "Дизайн":                  "ui ux design figma",
    "Тексты и переводы":       "copywriting translation russian",
    "Маркетинг и реклама":     "digital marketing seo",
    "SEO и трафик":            "seo traffic optimization",
    "Аудио и видео":           "video editing audio production",
}

_BASE_URL = "https://www.upwork.com/ab/feed/jobs/rss"


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", unescape(text))).strip()


def _extract_budget(description: str) -> str:
    m = _BUDGET_RE.search(description)
    return m.group(1).strip() if m else ""


def _job_key(url: str) -> str:
    m = _JOB_KEY_RE.search(url)
    return m.group(1) if m else url.rstrip("/").split("/")[-1][:EXT_ID_FALLBACK_LIMIT]


class UpworkParser(BaseParser):
    platform_name = "Upwork"
    base_url = "https://www.upwork.com"

    async def parse(self) -> list[ParsedOrder]:
        # Fetch all category feeds concurrently.
        feeds = list(_CATEGORY_QUERIES.items())
        tasks = [self._fetch_feed(cat, query) for cat, query in feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        raw: list[ParsedOrder] = []
        seen_ids: set[str] = set()
        for cat_query, result in zip(feeds, results):
            category, _ = cat_query
            if isinstance(result, Exception):
                logger.warning("[Upwork] Feed error for %s: %s", category, result)
                continue
            for order in result:
                if order.external_id not in seen_ids:
                    seen_ids.add(order.external_id)
                    raw.append(order)

        result_obj = filter_orders(raw)
        if result_obj.dropped:
            logger.info(
                "[Upwork] Filtered %d junk items: %s",
                result_obj.dropped, result_obj.reasons,
            )
        return result_obj.passed

    async def _fetch_feed(self, category: str, query: str) -> list[ParsedOrder]:
        params = urlencode({"q": query, "sort": "recency", "paging": "0;10"})
        url = f"{_BASE_URL}?{params}"
        xml = await self.fetch(url)
        if not xml:
            return []

        raw: list[ParsedOrder] = []
        soup = BeautifulSoup(xml, "lxml-xml")

        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue

                link_el = item.find("link")
                link = (link_el.get_text(strip=True) if link_el else "").split("?")[0]

                ext_id = _job_key(link) if link else title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = item.find("description")
                raw_desc = _strip_html(desc_el.get_text() if desc_el else "")

                price = _extract_budget(raw_desc)

                # Remove the budget line from the description to avoid redundancy.
                description = _BUDGET_RE.sub("", raw_desc).strip()

                raw.append(ParsedOrder(
                    platform=self.platform_name,
                    external_id=str(ext_id),
                    title=title,
                    description=description[:ORDER_DESCRIPTION_STORED],
                    category=category,
                    price=price,
                    url=link,
                ))
            except Exception:
                logger.debug("[Upwork] Item parse error", exc_info=True)

        return raw
