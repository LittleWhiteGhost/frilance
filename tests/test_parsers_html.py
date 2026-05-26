"""Snapshot tests for the HTML-scrape parsers (FL.ru, Freelance.ru, Weblancer).

The Kwork parser already has its own RSS-fixture test in test_kwork_parser.py.
These tests cover the three HTML-scrape parsers — the ones most likely to silently
return zero results when the upstream site redesigns its markup. The fixtures
under `tests/fixtures/*.html` are intentionally minimal: they exercise the CSS
selectors the parsers actually rely on and nothing else. When a fixture stops
matching, you know the parser needs an update before the upstream site goes
live with the change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bot.parsers.fl_ru import FLParser
from bot.parsers.habr_freelance import FreelanceRuParser
from bot.parsers.weblancer import WeblancerParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _run_parse(parser, html: str):
    """Run a parser against a canned HTML string and return its orders."""
    loop = asyncio.new_event_loop()
    try:
        with patch.object(parser, "fetch", new=AsyncMock(return_value=html)):
            return loop.run_until_complete(parser.parse())
    finally:
        loop.run_until_complete(parser.close())
        loop.close()


# ── FL.ru ────────────────────────────────────────────────────────────────────


class TestFLParser:
    def test_parses_known_cards(self):
        html = (FIXTURE_DIR / "fl_ru.html").read_text(encoding="utf-8")
        orders = _run_parse(FLParser(), html)
        assert len(orders) == 2
        first = orders[0]
        assert first.platform == "FL.ru"
        assert first.title.startswith("Нужен сайт на Django")
        # Relative URL is prefixed with base_url.
        assert first.url.startswith("https://www.fl.ru/projects/123456")
        # external_id derived from the URL tail.
        assert "123456" in first.external_id
        # Price comes from the b-post__price block.
        assert "30" in first.price and "000" in first.price
        # Description is captured (and length-clamped — sanity-check both).
        assert "Django" in first.description
        assert len(first.description) <= 500

    def test_absolute_url_kept_intact(self):
        html = (FIXTURE_DIR / "fl_ru.html").read_text(encoding="utf-8")
        orders = _run_parse(FLParser(), html)
        absolute = next(o for o in orders if o.title.startswith("Логотип"))
        # The fixture's second card uses a full https URL — should NOT be
        # double-prefixed with base_url.
        assert absolute.url.startswith("https://www.fl.ru/")
        assert "https://www.fl.ru/https://" not in absolute.url

    def test_empty_html_returns_empty(self):
        orders = _run_parse(FLParser(), "")
        assert orders == []

    def test_missing_html_returns_empty(self):
        loop = asyncio.new_event_loop()
        parser = FLParser()
        try:
            with patch.object(parser, "fetch", new=AsyncMock(return_value=None)):
                orders = loop.run_until_complete(parser.parse())
        finally:
            loop.run_until_complete(parser.close())
            loop.close()
        assert orders == []


# ── Freelance.ru ─────────────────────────────────────────────────────────────


class TestFreelanceRuParser:
    def test_parses_known_cards(self):
        html = (FIXTURE_DIR / "freelance_ru.html").read_text(encoding="utf-8")
        orders = _run_parse(FreelanceRuParser(), html)
        assert len(orders) == 2

        bot_order = next(o for o in orders if "Telegram" in o.title)
        assert bot_order.platform == "Freelance.ru"
        assert bot_order.category == "Программирование"
        assert "Python" in bot_order.title
        # Relative URL gets prefixed and the `.html` tail is stripped from the
        # external_id.
        assert bot_order.url.startswith("https://freelance.ru/project/")
        assert ".html" not in bot_order.external_id

    def test_empty_html_returns_empty(self):
        orders = _run_parse(FreelanceRuParser(), "<html><body></body></html>")
        assert orders == []


# ── Weblancer ────────────────────────────────────────────────────────────────


class TestWeblancerParser:
    def test_parses_known_cards(self):
        html = (FIXTURE_DIR / "weblancer.html").read_text(encoding="utf-8")
        orders = _run_parse(WeblancerParser(), html)
        # 2 real articles + 1 promo article without a usable link.
        # The promo card has neither `h2 a` nor `a.link-style`, so the parser
        # must drop it silently.
        assert len(orders) == 2

        react = next(o for o in orders if "SPA" in o.title)
        assert react.platform == "Weblancer"
        # Tags joined into the category field; first two tags expected.
        assert "Сайты" in react.category or "React" in react.category
        # Price is parsed from .text-green-600.
        assert "80" in react.price and "000" in react.price

    def test_absolute_url_kept_intact(self):
        html = (FIXTURE_DIR / "weblancer.html").read_text(encoding="utf-8")
        orders = _run_parse(WeblancerParser(), html)
        absolute = next(o for o in orders if "Android" in o.title)
        assert absolute.url.startswith("https://www.weblancer.net/")
        assert "https://www.weblancer.net/https://" not in absolute.url

    def test_promotional_article_without_link_is_skipped(self):
        """Articles that don't contain a job link must not be smuggled into
        the result set — otherwise we'd spam users with ads."""
        html = """
        <html><body>
          <article class="promo"><p>реклама внутри article без ссылки</p></article>
        </body></html>
        """
        orders = _run_parse(WeblancerParser(), html)
        assert orders == []
