"""Base classes shared by all platform parsers.

The class implements:
* a single long-lived `aiohttp.ClientSession` per parser (not per request);
* exponential-backoff retries for transient HTTP errors;
* User-Agent rotation to avoid trivial fingerprinting;
* `safe_parse()` that swallows exceptions and returns an empty list, so a
  failure in one parser doesn't take down the whole tick.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bot.constants import (
    HTTP_BACKOFF_BASE,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 "
    "Firefox/124.0",
]

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}


def _build_headers() -> dict[str, str]:
    headers = dict(_BASE_HEADERS)
    headers["User-Agent"] = random.choice(_USER_AGENTS)
    return headers


@dataclass
class ParsedOrder:
    platform: str
    external_id: str
    title: str
    description: str
    category: str
    price: str
    url: str

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "external_id": self.external_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "price": self.price,
            "url": self.url,
        }


class BaseParser(ABC):
    platform_name: str = ""
    base_url: str = ""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._external_session = session is not None
        self._session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._external_session = False
        return self._session

    async def close(self) -> None:
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "BaseParser":
        await self._get_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _request(
        self, url: str, params: dict | None, *, as_json: bool
    ) -> str | dict | list | None:
        session = await self._get_session()
        last_error: Exception | None = None

        for attempt in range(HTTP_MAX_RETRIES):
            try:
                async with session.get(url, params=params, headers=_build_headers()) as resp:
                    if resp.status == 200:
                        return await (resp.json() if as_json else resp.text())
                    if resp.status in (429, 502, 503, 504):
                        # Server told us to slow down or had a transient issue.
                        await self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                        continue
                    logger.warning(
                        "[%s] HTTP %s for %s", self.platform_name, resp.status, url,
                    )
                    return None
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning("[%s] Timeout for %s (attempt %s)", self.platform_name, url, attempt + 1)
            except aiohttp.ClientError as exc:
                last_error = exc
                logger.warning("[%s] Network error for %s: %s", self.platform_name, url, exc)
            except Exception:
                logger.exception("[%s] Unexpected error fetching %s", self.platform_name, url)
                return None

            await self._sleep_backoff(attempt, None)

        if last_error:
            logger.error("[%s] Giving up on %s: %s", self.platform_name, url, last_error)
        return None

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = HTTP_BACKOFF_BASE ** (attempt + 1)
        else:
            wait = HTTP_BACKOFF_BASE ** (attempt + 1)
        # add a tiny jitter so multiple parsers don't synchronise.
        wait += random.uniform(0, 0.5)
        await asyncio.sleep(wait)

    async def fetch(self, url: str, params: dict | None = None) -> str | None:
        result = await self._request(url, params, as_json=False)
        return result if isinstance(result, str) else None

    async def fetch_json(self, url: str, params: dict | None = None) -> dict | list | None:
        result = await self._request(url, params, as_json=True)
        if isinstance(result, (dict, list)):
            return result
        return None

    @abstractmethod
    async def parse(self) -> list[ParsedOrder]:
        ...

    async def safe_parse(self) -> list[ParsedOrder]:
        try:
            orders = await self.parse()
        except Exception:
            logger.exception("[%s] Parse error", self.platform_name)
            return []
        logger.info("[%s] Parsed %s orders", self.platform_name, len(orders))
        return orders
