"""Snapshot test for the Kwork RSS parser. We feed in a tiny canned RSS feed
and assert that the resulting `ParsedOrder` objects look right.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.parsers.kwork import KworkParser

FIXTURE = Path(__file__).parent / "fixtures" / "kwork_rss.xml"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_parses_kwork_rss_fixture():
    parser = KworkParser()
    fixture = FIXTURE.read_text(encoding="utf-8")

    with patch.object(parser, "fetch", new=AsyncMock(return_value=fixture)):
        loop = asyncio.new_event_loop()
        try:
            orders = loop.run_until_complete(parser.parse())
        finally:
            loop.run_until_complete(parser.close())
            loop.close()

    assert len(orders) == 2

    first = orders[0]
    assert first.platform == "Kwork"
    assert first.external_id == "12345"
    assert first.title.startswith("Нужен сайт на Django")
    # Description has its HTML decoded and tags stripped.
    assert "<p>" not in first.description
    assert "<b>" not in first.description
    assert "опытный" in first.description
    # Price is parsed out of the title.
    assert "5 000" in first.price.replace("\xa0", " ")
    # Category list joins the first two <category> tags.
    assert "Веб-разработка" in first.category

    # Query strings are stripped from URLs.
    assert orders[1].url == "https://kwork.ru/projects/67890"


def test_handles_empty_feed():
    parser = KworkParser()

    with patch.object(parser, "fetch", new=AsyncMock(return_value=None)):
        loop = asyncio.new_event_loop()
        try:
            orders = loop.run_until_complete(parser.parse())
        finally:
            loop.run_until_complete(parser.close())
            loop.close()

    assert orders == []
