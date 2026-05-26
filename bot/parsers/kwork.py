"""Kwork parser. The site is JS-rendered for the listing pages, so we use the
public RSS feed instead. Each `<item>` carries an HTML-escaped description we
need to decode once and strip tags from.
"""

from __future__ import annotations

import logging
import re
from html import unescape

from bs4 import BeautifulSoup

from bot.constants import EXT_ID_FALLBACK_LIMIT, ORDER_DESCRIPTION_STORED
from bot.parsers.base import BaseParser, ParsedOrder

logger = logging.getLogger(__name__)

# matches "за 1 500 руб" or "за 500 руб." in the title
_PRICE_RE = re.compile(r"за\s+([\d\s]+)\s*руб", re.IGNORECASE)
# strip leftover HTML tags after `unescape`
_TAG_RE = re.compile(r"<[^>]+>")
# collapse whitespace
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Decode entities and remove tags without a second BeautifulSoup pass."""
    if not text:
        return ""
    decoded = unescape(text)
    decoded = _TAG_RE.sub(" ", decoded)
    return _WS_RE.sub(" ", decoded).strip()


class KworkParser(BaseParser):
    """Parses Kwork via its public RSS feed."""

    platform_name = "Kwork"
    base_url = "https://kwork.ru"

    async def parse(self) -> list[ParsedOrder]:
        orders: list[ParsedOrder] = []
        url = f"{self.base_url}/rss"

        xml = await self.fetch(url)
        if not xml:
            return orders

        # `lxml-xml` parses RSS without the html re-parsing pass.
        soup = BeautifulSoup(xml, "lxml-xml")

        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue

                link_el = item.find("link")
                link = link_el.get_text(strip=True) if link_el else ""
                if "?" in link:
                    link = link.split("?")[0]

                ext_id = link.rstrip("/").split("/")[-1] if link else title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = item.find("description")
                description = _strip_html(desc_el.get_text() if desc_el else "")

                price_match = _PRICE_RE.search(title)
                price = (
                    _WS_RE.sub(" ", price_match.group(1)).strip() + " руб."
                    if price_match
                    else ""
                )

                cats = item.find_all("category")
                category = ", ".join(c.get_text(strip=True) for c in cats[:2])

                orders.append(ParsedOrder(
                    platform=self.platform_name,
                    external_id=str(ext_id),
                    title=title,
                    description=description[:ORDER_DESCRIPTION_STORED],
                    category=category,
                    price=price,
                    url=link,
                ))
            except Exception:
                logger.debug("[Kwork] Error parsing item", exc_info=True)
                continue

        return orders
