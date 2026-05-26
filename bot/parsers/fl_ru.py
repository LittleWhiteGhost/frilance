import logging

from bs4 import BeautifulSoup

from bot.constants import ORDER_DESCRIPTION_STORED, EXT_ID_FALLBACK_LIMIT
from bot.parsers.base import BaseParser, ParsedOrder

logger = logging.getLogger(__name__)


class FLParser(BaseParser):
    platform_name = "FL.ru"
    base_url = "https://www.fl.ru"

    async def parse(self) -> list[ParsedOrder]:
        orders: list[ParsedOrder] = []
        url = f"{self.base_url}/projects/"

        html = await self.fetch(url)
        if not html:
            return orders

        soup = BeautifulSoup(html, "lxml")

        cards = soup.select("div[id^='project-item']")
        if not cards:
            cards = soup.select("div.b-post")
        if not cards:
            cards = soup.select("div[class*='project-item']")

        for card in cards:
            try:
                title_el = card.select_one("h2 a")
                if not title_el:
                    title_el = card.select_one("a.b-post__link")
                if not title_el:
                    title_el = card.select_one("a[class*='title']")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                link = title_el.get("href", "")
                if link and not link.startswith("http"):
                    link = self.base_url + link

                ext_id = link.rstrip("/").split("/")[-1] if link else title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = card.select_one("div.b-post__body")
                if not desc_el:
                    desc_el = card.select_one("div[class*='text']")
                description = desc_el.get_text(strip=True) if desc_el else ""

                price_el = card.select_one("div.b-post__price")
                if not price_el:
                    price_el = card.select_one("span[class*='budget']")
                if not price_el:
                    price_el = card.select_one("div[class*='price']")
                price = price_el.get_text(strip=True) if price_el else ""

                cat_el = card.select_one("span[class*='category']")
                if not cat_el:
                    cat_el = card.select_one("a[class*='category']")
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
                logger.debug("[FL.ru] Error parsing card", exc_info=True)
                continue

        return orders
