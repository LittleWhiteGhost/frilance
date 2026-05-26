"""Tests for the YooKassa webhook IP allowlist (defence-in-depth)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import database as db_mod
from bot.payments.yookassa_ips import (
    DEFAULT_YOOKASSA_CIDRS,
    ip_is_allowed,
    parse_allowlist,
)


# ── parse_allowlist ─────────────────────────────────────────────────────────


class TestParseAllowlist:
    def test_empty_returns_empty_list(self):
        assert parse_allowlist(None) == []
        assert parse_allowlist("") == []
        assert parse_allowlist("   ") == []

    def test_default_keyword_expands_to_published_list(self):
        out = parse_allowlist("default")
        assert len(out) == len(DEFAULT_YOOKASSA_CIDRS)

    def test_custom_cidr_list(self):
        out = parse_allowlist("10.0.0.0/8, 192.168.1.5")
        assert len(out) == 2

    def test_bare_ip_is_promoted_to_host_route(self):
        out = parse_allowlist("8.8.8.8")
        assert len(out) == 1
        assert str(out[0]) == "8.8.8.8/32"

    def test_invalid_entry_skipped_not_fatal(self):
        # A typo in one entry must NOT discard the rest of the list — otherwise
        # a typo in env blackholes the entire endpoint.
        out = parse_allowlist("10.0.0.0/8, notanip, 192.168.0.1")
        assert len(out) == 2


# ── ip_is_allowed ───────────────────────────────────────────────────────────


class TestIpIsAllowed:
    def test_empty_allowlist_is_always_allow(self):
        assert ip_is_allowed("203.0.113.1", []) is True

    def test_in_range_allowed(self):
        nets = parse_allowlist("10.0.0.0/8")
        assert ip_is_allowed("10.1.2.3", nets) is True

    def test_out_of_range_blocked(self):
        nets = parse_allowlist("10.0.0.0/8")
        assert ip_is_allowed("8.8.8.8", nets) is False

    def test_default_yookassa_range_includes_published_ips(self):
        nets = parse_allowlist("default")
        # Smoke-check a handful of host IPs that *must* be in the published
        # range. These fail loudly if anyone edits the constants by mistake.
        assert ip_is_allowed("185.71.76.1", nets) is True
        assert ip_is_allowed("77.75.156.11", nets) is True
        # Outside the range:
        assert ip_is_allowed("1.2.3.4", nets) is False

    def test_ipv6_supported(self):
        nets = parse_allowlist("2a02:5180:0:1509::/64")
        assert ip_is_allowed("2a02:5180:0:1509::42", nets) is True
        assert ip_is_allowed("2a02:5180:0:1510::1", nets) is False

    def test_garbage_input_fails_closed(self):
        nets = parse_allowlist("10.0.0.0/8")
        # If we can't parse the source IP, default to "blocked" so a missing
        # remote address can't slip past a configured allowlist.
        assert ip_is_allowed("not an ip", nets) is False
        assert ip_is_allowed("", nets) is False


# ── webhook integration ─────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    async def _setup(path: str):
        old = db_mod.db
        db_mod.db = db_mod.Database(path)
        await db_mod.init_db()
        return old

    async def _teardown(old):
        try:
            await db_mod.close_db()
        finally:
            db_mod.db = old

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wh.db")
        loop = asyncio.new_event_loop()
        try:
            old = loop.run_until_complete(_setup(path))
            yield loop
            loop.run_until_complete(_teardown(old))
        finally:
            loop.close()


def _make_request(remote: str, headers: dict | None = None, body: dict | None = None):
    """Minimal aiohttp request stub for the webhook handler."""
    req = MagicMock()
    req.remote = remote
    req.headers = headers or {}
    req.json = AsyncMock(return_value=body or {})
    return req


class TestWebhookAllowlistIntegration:
    def test_request_from_allowed_ip_is_processed(self, temp_db):
        from bot.payments.webhook import _handle_yookassa_webhook

        async def _run():
            with patch.object(
                __import__("bot.config", fromlist=["config"]).config,
                "yookassa_webhook_ip_allowlist",
                "10.0.0.0/8",
            ):
                # Ensure proxy trust is OFF for this test so request.remote
                # is what matters.
                with patch.object(
                    __import__("bot.config", fromlist=["config"]).config,
                    "yookassa_webhook_trust_proxy",
                    False,
                ):
                    req = _make_request(
                        "10.1.2.3",
                        body={"event": "payment.succeeded", "object": {}},
                    )
                    resp = await _handle_yookassa_webhook(req)
                    # Allowed → passes IP check, then bails on missing
                    # payment_id with a 400, NOT a 403.
                    assert resp.status == 400

        temp_db.run_until_complete(_run())

    def test_request_from_blocked_ip_is_403(self, temp_db):
        from bot.payments.webhook import _handle_yookassa_webhook

        async def _run():
            with patch.object(
                __import__("bot.config", fromlist=["config"]).config,
                "yookassa_webhook_ip_allowlist",
                "10.0.0.0/8",
            ):
                with patch.object(
                    __import__("bot.config", fromlist=["config"]).config,
                    "yookassa_webhook_trust_proxy",
                    False,
                ):
                    req = _make_request(
                        "8.8.8.8",
                        body={"event": "payment.succeeded"},
                    )
                    resp = await _handle_yookassa_webhook(req)
                    assert resp.status == 403
                    # And we never even bothered calling .json() on the body —
                    # blocking happens BEFORE any body parsing.
                    req.json.assert_not_called()

        temp_db.run_until_complete(_run())

    def test_empty_allowlist_skips_check(self, temp_db):
        """Default config: no allowlist → any IP is accepted."""
        from bot.payments.webhook import _handle_yookassa_webhook

        async def _run():
            with patch.object(
                __import__("bot.config", fromlist=["config"]).config,
                "yookassa_webhook_ip_allowlist",
                "",
            ):
                req = _make_request(
                    "8.8.8.8",
                    body={"event": "payment.succeeded", "object": {}},
                )
                resp = await _handle_yookassa_webhook(req)
                # No allowlist → IP check skipped, falls through to "missing
                # payment id" → 400, not 403.
                assert resp.status == 400

        temp_db.run_until_complete(_run())

    def test_proxy_mode_uses_x_forwarded_for_leftmost(self, temp_db):
        from bot.payments.webhook import _handle_yookassa_webhook

        async def _run():
            with patch.object(
                __import__("bot.config", fromlist=["config"]).config,
                "yookassa_webhook_ip_allowlist",
                "10.0.0.0/8",
            ), patch.object(
                __import__("bot.config", fromlist=["config"]).config,
                "yookassa_webhook_trust_proxy",
                True,
            ):
                # Remote is the proxy (172.x); XFF says original client was
                # 10.1.2.3 (allowed). Expect: allowed.
                req = _make_request(
                    "172.16.0.1",
                    headers={"X-Forwarded-For": "10.1.2.3, 172.16.0.1"},
                    body={"event": "payment.succeeded"},
                )
                resp = await _handle_yookassa_webhook(req)
                # IP check passed → falls through to body validation → 400 on
                # missing object/id.
                assert resp.status == 400

                # Now flip the XFF to a blocked IP — must be 403.
                req2 = _make_request(
                    "172.16.0.1",
                    headers={"X-Forwarded-For": "8.8.8.8, 172.16.0.1"},
                    body={"event": "payment.succeeded"},
                )
                resp2 = await _handle_yookassa_webhook(req2)
                assert resp2.status == 403

        temp_db.run_until_complete(_run())
