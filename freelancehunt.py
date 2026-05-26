"""Freelancehunt.com parser.

Freelancehunt exposes a public RSS feed at /projects/feed that is far more
stable than their HTML listing. We use RSS as the primary source and fall back
to HTML scraping only when the feed is unavailable.

RSS item format (stable as of mid-2026):
  <title>Разработка чат-бота [Python, Telegram]</title>
  <link>https://freelancehunt.com/project/12345/...</link>
  <description>HTML-escaped description</description>
  <category>Программирование</category>
  <fh:budget>5000 UAH</fh:budget>   ← sometimes present

Ukrainian platform — prices in UAH, but Russian-speaking audience is large.
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
# Freelancehunt project URLs: /project/<id>/slug
_ID_FROM_URL_RE = re.compile(r"/project/(\d+)")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", unescape(text))).strip()


def _ext_id_from_url(url: str) -> str:
    m = _ID_FROM_URL_RE.search(url)
    return m.group(1) if m else url.rstrip("/").split("/")[-1]


class FreelancehuntParser(BaseParser):
    platform_name = "Freelancehunt"
    base_url = "https://freelancehunt.com"

    async def parse(self) -> list[ParsedOrder]:
        orders = await self._parse_rss()
        if not orders:
            logger.info("[Freelancehunt] RSS empty, trying HTML fallback")
            orders = await self._parse_html()
        result = filter_orders(orders)
        if result.dropped:
            logger.info(
                "[Freelancehunt] Filtered %d junk items: %s",
                result.dropped, result.reasons,
            )
        return result.passed

    # ── RSS (primary) ──────────────────────────────────────────────────────

    async def _parse_rss(self) -> list[ParsedOrder]:
        raw: list[ParsedOrder] = []
        xml = await self.fetch(f"{self.base_url}/projects/feed")
        if not xml:
            return raw

        soup = BeautifulSoup(xml, "lxml-xml")
        for item in soup.find_all("item"):
            try:
                title_el = item.find("title")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue

                link_el = item.find("link")
                link = (link_el.get_text(strip=True) if link_el else "").split("?")[0]

                ext_id = _ext_id_from_url(link) if link else title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = item.find("description")
                description = _strip_html(desc_el.get_text() if desc_el else "")

                # Budget tag: <fh:budget> or a <budget> element
                budget_el = item.find("fh:budget") or item.find("budget")
                price = budget_el.get_text(strip=True) if budget_el else ""

                cats = item.find_all("category")
                category = ", ".join(c.get_text(strip=True) for c in cats[:2])

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
                logger.debug("[Freelancehunt] RSS item parse error", exc_info=True)
        return raw

    # ── HTML fallback ──────────────────────────────────────────────────────

    async def _parse_html(self) -> list[ParsedOrder]:
        raw: list[ParsedOrder] = []
        html = await self.fetch(f"{self.base_url}/projects/")
        if not html:
            return raw

        soup = BeautifulSoup(html, "lxml")
        # Freelancehunt wraps each project in <div class="project-card"> or
        # a <tr class="project"> depending on view mode.
        cards = soup.select("div.project-card, tr.project")

        for card in cards:
            try:
                title_el = card.select_one("a.project-name, h2 a, td.name a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                link = title_el.get("href", "")
                if link and not link.startswith("http"):
                    link = self.base_url + link
                link = link.split("?")[0]

                if "/project/" not in link:
                    continue

                ext_id = _ext_id_from_url(link) or title[:EXT_ID_FALLBACK_LIMIT]

                desc_el = card.select_one("div.description, p.description, .text")
                description = desc_el.get_text(strip=True) if desc_el else ""

                price_el = card.select_one(
                    "span.budget, div.budget, .price, [class*='budget']"
                )
                price = price_el.get_text(strip=True) if price_el else ""

                cat_el = card.select_one("span.tag, a.tag, .skill, [class*='skill']")
                category = cat_el.get_text(strip=True) if cat_el else ""

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
                logger.debug("[Freelancehunt] HTML card parse error", exc_info=True)
        return raw
