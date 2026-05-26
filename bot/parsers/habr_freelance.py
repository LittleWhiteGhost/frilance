import logging

from bs4 import BeautifulSoup

from bot.constants import EXT_ID_FALLBACK_LIMIT, ORDER_DESCRIPTION_STORED
from bot.parsers.base import BaseParser, ParsedOrder

logger = logging.getLogger(__name__)


class FreelanceRuParser(BaseParser):
    """Freelance.ru — replacement for Habr Freelance (shut down, HTTP 410)."""

    platform_name = "Freelance.ru"
    base_url = "https://freelance.ru"

    async def parse(self) -> list[ParsedOrder]:
        orders: list[ParsedOrder] = []
        url = f"{self.base_url}/project/search"

        html = await self.fetch(url)
        if not html:
            return orders

        soup = BeautifulSoup(html, "lxml")

        cards = soup.select("div.project-item-default-card")

        for card in cards:
            try:
                title_el = card.select_one("h2.title a")
                if not title_el:
                    title_el = card.select_one("h2 a")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                link = title_el.get("href", "")
                if link and not link.startswith("http"):
                    link = self.base_url + link

                ext_id = (
                    link.rstrip("/").split("/")[-1].replace(".html", "")
                    if link
                    else title[:EXT_ID_FALLBACK_LIMIT]
                )

                desc_el = card.select_one("a.description")
                if not desc_el:
                    desc_el = card.select_one("div.description")
                description = desc_el.get_text(strip=True) if desc_el else ""

                price_el = card.select_one("div.cost")
                price = price_el.get_text(strip=True) if price_el else ""

                cat_el = card.select_one("div.specs-list b")
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
                logger.debug("[Freelance.ru] Error parsing card", exc_info=True)
                continue

        return orders
