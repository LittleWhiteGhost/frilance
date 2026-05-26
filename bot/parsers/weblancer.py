import logging

from bs4 import BeautifulSoup

from bot.constants import EXT_ID_FALLBACK_LIMIT, ORDER_DESCRIPTION_STORED
from bot.parsers.base import BaseParser, ParsedOrder

logger = logging.getLogger(__name__)


class WeblancerParser(BaseParser):
    platform_name = "Weblancer"
    base_url = "https://www.weblancer.net"

    async def parse(self) -> list[ParsedOrder]:
        orders: list[ParsedOrder] = []
        url = f"{self.base_url}/jobs/"

        html = await self.fetch(url)
        if not html:
            return orders

        soup = BeautifulSoup(html, "lxml")

        cards = soup.select("article")

        for card in cards:
            try:
                title_el = card.select_one("h2 a")
                if not title_el:
                    title_el = card.select_one("a.link-style")
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                link = title_el.get("href", "")
                if link and not link.startswith("http"):
                    link = self.base_url + link

                ext_id = link.rstrip("/").split("/")[-1] if link else title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = card.select_one("p.text-gray-600")
                if not desc_el:
                    desc_el = card.select_one("p")
                description = desc_el.get_text(strip=True) if desc_el else ""

                price_el = card.select_one("span.text-green-600")
                if not price_el:
                    price_el = card.select_one("span[class*='text-green']")
                price = price_el.get_text(strip=True) if price_el else ""

                tag_links = card.select("div.flex.flex-wrap a[href*='/freelance/']")
                if not tag_links:
                    tag_links = [
                        a for a in card.select("a[href*='/freelance/']")
                        if a != title_el and a.get("href", "") != link.replace(self.base_url, "")
                    ]
                category = ", ".join(t.get_text(strip=True) for t in tag_links[:3])

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
                logger.debug("[Weblancer] Error parsing card", exc_info=True)
                continue

        return orders
