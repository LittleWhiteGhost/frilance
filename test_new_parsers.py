"""Tests for new parsers (Freelancehunt, HabrCareer, Upwork)
and updated delivery-speed constants.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("YOOKASSA_SHOP_ID", "test")
os.environ.setdefault("YOOKASSA_SECRET_KEY", "test")

import pytest
from bot.constants import (
    TIER_BASIC, TIER_PRO, TIER_MAX,
    TIER_DELIVERY_COOLDOWN_SECONDS,
    TIER_ORDERS_PER_TICK,
    PLATFORM_REGISTRY,
)


# ── Delivery speed constants ─────────────────────────────────────────────────

class TestDeliverySpeed:
    def test_max_cooldown_is_30_seconds(self):
        assert TIER_DELIVERY_COOLDOWN_SECONDS[TIER_MAX] == 30

    def test_pro_cooldown_is_1_minute(self):
        assert TIER_DELIVERY_COOLDOWN_SECONDS[TIER_PRO] == 60

    def test_basic_cooldown_is_3_minutes(self):
        assert TIER_DELIVERY_COOLDOWN_SECONDS[TIER_BASIC] == 180

    def test_max_faster_than_pro_faster_than_basic(self):
        assert (
            TIER_DELIVERY_COOLDOWN_SECONDS[TIER_MAX]
            < TIER_DELIVERY_COOLDOWN_SECONDS[TIER_PRO]
            < TIER_DELIVERY_COOLDOWN_SECONDS[TIER_BASIC]
        )

    def test_orders_per_tick_strictly_increasing(self):
        assert (
            TIER_ORDERS_PER_TICK[TIER_BASIC]
            < TIER_ORDERS_PER_TICK[TIER_PRO]
            < TIER_ORDERS_PER_TICK[TIER_MAX]
        )

    def test_max_gets_100_orders_per_tick(self):
        assert TIER_ORDERS_PER_TICK[TIER_MAX] == 100

    def test_pro_gets_30_orders_per_tick(self):
        assert TIER_ORDERS_PER_TICK[TIER_PRO] == 30

    def test_basic_gets_10_orders_per_tick(self):
        assert TIER_ORDERS_PER_TICK[TIER_BASIC] == 10


# ── Platform registry ────────────────────────────────────────────────────────

class TestPlatformRegistry:
    def test_all_8_platforms_registered(self):
        assert len(PLATFORM_REGISTRY) == 8

    def test_new_platforms_present(self):
        assert "freelancehunt" in PLATFORM_REGISTRY
        assert "habr_career" in PLATFORM_REGISTRY
        assert "upwork" in PLATFORM_REGISTRY

    def test_each_platform_has_required_fields(self):
        required = {"name", "emoji", "url", "notes"}
        for code, meta in PLATFORM_REGISTRY.items():
            missing = required - set(meta.keys())
            assert not missing, f"{code} missing fields: {missing}"

    def test_platform_names_unique(self):
        names = [m["name"] for m in PLATFORM_REGISTRY.values()]
        assert len(names) == len(set(names))


# ── Freelancehunt parser ─────────────────────────────────────────────────────

FREELANCEHUNT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Freelancehunt Projects</title>
    <item>
      <title>Разработка Telegram-бота на Python</title>
      <link>https://freelancehunt.com/project/12345/telegram-bot.html</link>
      <description>&lt;p&gt;Нужен опытный Python-разработчик.&lt;/p&gt;</description>
      <category>Программирование</category>
      <budget>5 000 UAH</budget>
    </item>
    <item>
      <title>Дизайн логотипа для кофейни</title>
      <link>https://freelancehunt.com/project/67890/logo.html</link>
      <description>&lt;p&gt;Минималистичный логотип.&lt;/p&gt;</description>
      <category>Дизайн</category>
    </item>
  </channel>
</rss>"""


class TestFreelancehuntParser:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_parses_rss_items(self):
        from bot.parsers.freelancehunt import FreelancehuntParser
        parser = FreelancehuntParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=FREELANCEHUNT_RSS)):
            orders = self._run(parser.parse())
        assert len(orders) == 2

    def test_platform_name_correct(self):
        from bot.parsers.freelancehunt import FreelancehuntParser
        parser = FreelancehuntParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=FREELANCEHUNT_RSS)):
            orders = self._run(parser.parse())
        assert all(o.platform == "Freelancehunt" for o in orders)

    def test_extracts_id_from_url(self):
        from bot.parsers.freelancehunt import FreelancehuntParser
        parser = FreelancehuntParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=FREELANCEHUNT_RSS)):
            orders = self._run(parser.parse())
        assert orders[0].external_id == "12345"
        assert orders[1].external_id == "67890"

    def test_strips_html_from_description(self):
        from bot.parsers.freelancehunt import FreelancehuntParser
        parser = FreelancehuntParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=FREELANCEHUNT_RSS)):
            orders = self._run(parser.parse())
        assert "<p>" not in orders[0].description

    def test_empty_feed_returns_empty(self):
        from bot.parsers.freelancehunt import FreelancehuntParser
        parser = FreelancehuntParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=None)):
            orders = self._run(parser.parse())
        assert orders == []


# ── HabrCareer parser ────────────────────────────────────────────────────────

HABR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Habr Career — Фриланс</title>
    <item>
      <title>Нужен разработчик на Go [от 100 000 руб]</title>
      <link>https://career.habr.com/freelance/projects/555</link>
      <description>&lt;p&gt;Микросервисы на Go, опыт от 3 лет.&lt;/p&gt;</description>
      <category>Программирование</category>
      <category>Go</category>
    </item>
    <item>
      <title>SEO-оптимизация сайта</title>
      <link>https://career.habr.com/freelance/projects/666</link>
      <description>&lt;p&gt;Нужен специалист по SEO.&lt;/p&gt;</description>
      <category>SEO</category>
    </item>
  </channel>
</rss>"""


class TestHabrCareerParser:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_parses_two_items(self):
        from bot.parsers.habr_career import HabrCareerParser
        parser = HabrCareerParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=HABR_RSS)):
            orders = self._run(parser.parse())
        assert len(orders) == 2

    def test_extracts_budget_from_title_brackets(self):
        from bot.parsers.habr_career import HabrCareerParser
        parser = HabrCareerParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=HABR_RSS)):
            orders = self._run(parser.parse())
        go_order = next(o for o in orders if "Go" in o.title)
        assert "100 000" in go_order.price
        # Budget bracket should be removed from the title itself.
        assert "[" not in go_order.title

    def test_extracts_numeric_id(self):
        from bot.parsers.habr_career import HabrCareerParser
        parser = HabrCareerParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=HABR_RSS)):
            orders = self._run(parser.parse())
        assert orders[0].external_id == "555"
        assert orders[1].external_id == "666"

    def test_multiple_categories_joined(self):
        from bot.parsers.habr_career import HabrCareerParser
        parser = HabrCareerParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=HABR_RSS)):
            orders = self._run(parser.parse())
        go_order = next(o for o in orders if "555" == o.external_id)
        assert "Программирование" in go_order.category
        assert "Go" in go_order.category

    def test_platform_name(self):
        from bot.parsers.habr_career import HabrCareerParser
        parser = HabrCareerParser()
        with patch.object(parser, "fetch", new=AsyncMock(return_value=HABR_RSS)):
            orders = self._run(parser.parse())
        assert all(o.platform == "Habr Career" for o in orders)


# ── Upwork parser ────────────────────────────────────────────────────────────

UPWORK_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Upwork Jobs</title>
    <item>
      <title>Python FastAPI Backend Developer</title>
      <link>https://www.upwork.com/jobs/Python-FastAPI-Backend_~01abc123def/</link>
      <description>
        We need a Python developer for a REST API project.
        Budget: $1,500
        Skills: Python, FastAPI, PostgreSQL
      </description>
    </item>
    <item>
      <title>React Frontend Developer Needed</title>
      <link>https://www.upwork.com/jobs/React-Frontend_~02xyz456/</link>
      <description>
        Hourly Range: $25-$40/hr
        Modern React app with TypeScript.
      </description>
    </item>
  </channel>
</rss>"""


class TestUpworkParser:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_parses_items(self):
        from bot.parsers.upwork import UpworkParser
        parser = UpworkParser()
        # Patch _fetch_feed directly to avoid multiple concurrent fetches in test.
        async def mock_fetch_feed(cat, query):
            from bot.parsers.upwork import _strip_html, _extract_budget, _job_key
            from bot.parsers.base import ParsedOrder
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(UPWORK_RSS, "lxml-xml")
            result = []
            for item in soup.find_all("item"):
                title = item.find("title").get_text(strip=True)
                link = item.find("link").get_text(strip=True)
                desc = _strip_html(item.find("description").get_text())
                result.append(ParsedOrder(
                    platform="Upwork", external_id=_job_key(link),
                    title=title, description=desc[:500], category=cat,
                    price=_extract_budget(desc), url=link,
                ))
            return result

        with patch.object(parser, "_fetch_feed", side_effect=mock_fetch_feed):
            orders = self._run(parser.parse())
        assert len(orders) >= 2

    def test_extracts_fixed_price(self):
        from bot.parsers.upwork import _extract_budget
        desc = "We need a developer.\nBudget: $1,500\nSkills: Python"
        assert "$1,500" in _extract_budget(desc)

    def test_extracts_hourly_range(self):
        from bot.parsers.upwork import _extract_budget
        desc = "Hourly Range: $25-$40/hr\nModern React app."
        assert "$25-$40" in _extract_budget(desc)

    def test_extracts_job_key_from_url(self):
        from bot.parsers.upwork import _job_key
        url = "https://www.upwork.com/jobs/Python-FastAPI_~01abc123def/"
        assert _job_key(url) == "01abc123def"

    def test_platform_name(self):
        from bot.parsers.upwork import UpworkParser
        assert UpworkParser.platform_name == "Upwork"
