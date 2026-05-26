import logging

from bs4 import BeautifulSoup

from bot.constants import EXT_ID_FALLBACK_LIMIT, ORDER_DESCRIPTION_STORED
from bot.parsers.base import BaseParser, ParsedOrder

logger = logging.getLogger(__name__)


class YouDoParser(BaseParser):
    """YouDo.com parser.

    Note: YouDo uses ServicePipe anti-bot protection which may block simple
    HTTP requests. The parser will return an empty list if blocked.
    Consider running via a residential proxy or disabling this parser
    if it consistently returns 0 results.
    """

    platform_name = "YouDo"
    base_url = "https://youdo.com"

    async def parse(self) -> list[ParsedOrder]:
        orders: list[ParsedOrder] = []

        url = f"{self.base_url}/tasks-all/"

        html = await self.fetch(url)
        if not html:
            return orders

        if len(html) < 5000 and "servicepipe" in html.lower():
            logger.warning("[YouDo] Anti-bot protection detected, skipping")
            return orders

        soup = BeautifulSoup(html, "lxml")

        cards = soup.select("a[class*='TaskItem']")
        if not cards:
            cards = soup.select("li[class*='list__item']")
        if not cards:
            cards = soup.select("div[class*='TaskCard']")

        for card in cards:
            try:
                title_el = card.select_one("h3")
                if not title_el:
                    title_el = card.select_one("div[class*='title']")
                if not title_el:
                    if card.name == "a":
                        title_el = card
                    else:
                        continue

                title = title_el.get_text(strip=True)

                link_el = card if card.name == "a" else card.select_one("a")
                link = ""
                if link_el:
                    link = link_el.get("href", "")
                    if link and not link.startswith("http"):
                        link = self.base_url + link

                ext_id = link.rstrip("/").split("/")[-1] if link else title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = card.select_one("div[class*='description']")
                if not desc_el:
                    desc_el = card.select_one("p")
                description = desc_el.get_text(strip=True) if desc_el else ""

                price_el = card.select_one("span[class*='price']")
                if not price_el:
                    price_el = card.select_one("span[class*='Price']")
                price = price_el.get_text(strip=True) if price_el else ""

                cat_el = card.select_one("span[class*='category']")
                category = cat_el.get_text(strip=True) if cat_el else ""

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
                logger.debug("[YouDo] Error parsing card", exc_info=True)
                continue

        return orders
