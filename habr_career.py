"""Habr Career freelance projects parser.

After Habr Freelance shut down (HTTP 410) the freelance project listings
moved to career.habr.com/freelance. The page exposes a public RSS feed at
/freelance/rss which is the most reliable ingestion method.

Feed URL: https://career.habr.com/freelance/rss
Each <item> contains:
  <title>        — project title with optional budget in brackets
  <link>         — canonical project URL (no UTM params in RSS)
  <description>  — HTML-escaped project description
  <category>     — skill tag (multiple elements)
  <pubDate>      — publication date (RFC 822)
"""

from __future__ import annotations

import logging
import re
from html import unescape

from bs4 import BeautifulSoup

from bot.constants import EXT_ID_FALLBACK_LIMIT, ORDER_DESCRIPTION_STORED
from bot.parsers.base import BaseParser, ParsedOrder
from bot.parsers.filters import filter_orders

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Budget patterns in titles: "[от 50 000 руб]", "[50 000 руб]", "[50k]"
_BUDGET_IN_TITLE_RE = re.compile(
    r"\[([^\]]*(?:руб|₽|rub|usd|\$|uah|грн|тыс|k)\b[^\]]*)\]",
    re.IGNORECASE,
)
# Habr project URL: /freelance/projects/<id>
_ID_FROM_URL_RE = re.compile(r"/freelance/projects/(\d+)")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", unescape(text))).strip()


class HabrCareerParser(BaseParser):
    platform_name = "Habr Career"
    base_url = "https://career.habr.com"

    async def parse(self) -> list[ParsedOrder]:
        raw: list[ParsedOrder] = []
        xml = await self.fetch(f"{self.base_url}/freelance/rss")
        if not xml:
            return raw

        soup = BeautifulSoup(xml, "lxml-xml")

        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                raw_title = title_el.get_text(strip=True) if title_el else ""
                if not raw_title:
                    continue

                # Extract budget from title brackets, then clean the title.
                budget_match = _BUDGET_IN_TITLE_RE.search(raw_title)
                price = budget_match.group(1).strip() if budget_match else ""
                title = _BUDGET_IN_TITLE_RE.sub("", raw_title).strip()

                link_el = item.find("link")
                link = (link_el.get_text(strip=True) if link_el else "").split("?")[0]
                if link and not link.startswith("http"):
                    link = self.base_url + link

                id_match = _ID_FROM_URL_RE.search(link)
                ext_id = id_match.group(1) if id_match else (
                    link.rstrip("/").split("/")[-1] or title[:EXT_ID_FALLBACK_LIMIT]
                )

                desc_el = item.find("description")
                description = _strip_html(desc_el.get_text() if desc_el else "")

                cats = item.find_all("category")
                category = ", ".join(c.get_text(strip=True) for c in cats[:3])

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
                logger.debug("[HabrCareer] Item parse error", exc_info=True)

        result = filter_orders(raw)
        if result.dropped:
            logger.info(
                "[HabrCareer] Filtered %d junk items: %s",
                result.dropped, result.reasons,
            )
        return result.passed
