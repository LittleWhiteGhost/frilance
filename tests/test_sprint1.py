"""Tests for the Sprint 1 reliability/monetization features:

* In-memory LRU dedup cache in `save_orders` (no double-roundtrips for
  already-seen external_ids).
* Schema v3 reminder/upsell columns + the helpers gating them.
* Per-platform zero-results streak tracking that pages admins.
* Cooperative graceful shutdown of the scheduler via stop_event.
* /cancel command that clears any FSM state.
* /health endpoint returns 200 only when the DB is reachable.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import database as db_mod
from bot import scheduler as scheduler_mod
from bot.constants import PARSER_ZERO_RESULTS_ALERT_STREAK, TIER_BASIC, TIER_PRO


# ── helpers ─────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_loop():
    async def _setup(path):
        old = db_mod.db
        db_mod.db = db_mod.Database(path)
        await db_mod.init_db()
        # Reset the LRU cache between tests so prior test data doesn't leak.
        db_mod._order_seen.clear()
        return old

    async def _teardown(old):
        try:
            await db_mod.close_db()
        finally:
            db_mod.db = old
            db_mod._order_seen.clear()

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sprint1.db")
        loop = asyncio.new_event_loop()
        try:
            old = loop.run_until_complete(_setup(path))
            yield loop
            loop.run_until_complete(_teardown(old))
        finally:
            loop.close()


def _order(platform: str, ext_id: str) -> dict:
    return {
        "platform": platform,
        "external_id": ext_id,
        "title": f"Order {ext_id}",
        "description": "",
        "category": "Программирование",
        "price": "",
        "url": f"https://example/{ext_id}",
    }


# ── LRU dedup cache ─────────────────────────────────────────────────────────


class TestOrderDedupCache:
    def test_second_call_with_same_id_is_skipped(self, temp_db_loop):
        async def _run():
            ids = await db_mod.save_orders([_order("Kwork", "abc-1")])
            assert len(ids) == 1
            # Second call with the same external_id must be a complete no-op.
            ids2 = await db_mod.save_orders([_order("Kwork", "abc-1")])
            assert ids2 == []
            # And only ONE row exists in the DB.
            cur = await db_mod.db.conn.execute("SELECT COUNT(*) AS c FROM orders")
            row = await cur.fetchone()
            assert row["c"] == 1

        temp_db_loop.run_until_complete(_run())

    def test_cache_is_per_platform(self, temp_db_loop):
        async def _run():
            # Same external_id on two different platforms should both insert.
            ids1 = await db_mod.save_orders([_order("Kwork", "shared-id")])
            ids2 = await db_mod.save_orders([_order("FL.ru", "shared-id")])
            assert len(ids1) == 1
            assert len(ids2) == 1

        temp_db_loop.run_until_complete(_run())

    def test_cache_evicts_oldest_when_capped(self, temp_db_loop):
        async def _run():
            # Tighten the cap to make the test fast and deterministic.
            old_cap = db_mod._ORDER_CACHE_SIZE
            db_mod._ORDER_CACHE_SIZE = 5
            try:
                db_mod._order_seen.clear()
                for i in range(10):
                    db_mod._cache_seen("Kwork", f"id-{i}")
                # Only the 5 most-recently-cached IDs survive.
                assert len(db_mod._order_seen) == 5
                assert ("Kwork", "id-0") not in db_mod._order_seen
                assert ("Kwork", "id-9") in db_mod._order_seen
            finally:
                db_mod._ORDER_CACHE_SIZE = old_cap

        temp_db_loop.run_until_complete(_run())


# ── reminders & upsell helpers ──────────────────────────────────────────────


class TestReminderHelpers:
    def test_3d_window_finds_subscription_in_3day_band(self, temp_db_loop):
        async def _run():
            uid = 100
            await db_mod.add_user(uid, "u", "U")
            # Activate a paid subscription that expires in ~3 days.
            await db_mod.db.conn.execute(
                "INSERT INTO subscriptions (user_id, plan, tier, expires_at) "
                "VALUES (?, 'monthly', ?, datetime('now', '+2 days', '+23 hours'))",
                (uid, TIER_PRO),
            )
            await db_mod.db.conn.commit()

            due = await db_mod.get_subscriptions_needing_reminder(3)
            assert len(due) == 1
            assert due[0]["user_id"] == uid

            # 1-day window should NOT pick this one up.
            due_1d = await db_mod.get_subscriptions_needing_reminder(1)
            assert due_1d == []

        temp_db_loop.run_until_complete(_run())

    def test_already_reminded_subscription_not_returned(self, temp_db_loop):
        async def _run():
            uid = 101
            await db_mod.add_user(uid, "u", "U")
            await db_mod.db.conn.execute(
                "INSERT INTO subscriptions "
                "(user_id, plan, tier, expires_at, reminder_3d_sent_at) "
                "VALUES (?, 'monthly', ?, datetime('now', '+2 days'), datetime('now'))",
                (uid, TIER_PRO),
            )
            await db_mod.db.conn.commit()

            due = await db_mod.get_subscriptions_needing_reminder(3)
            assert due == []

        temp_db_loop.run_until_complete(_run())

    def test_trial_subscriptions_excluded(self, temp_db_loop):
        async def _run():
            uid = 102
            await db_mod.add_user(uid, "u", "U")
            await db_mod.db.conn.execute(
                "INSERT INTO subscriptions (user_id, plan, tier, expires_at) "
                "VALUES (?, 'trial', ?, datetime('now', '+2 days'))",
                (uid, TIER_PRO),
            )
            await db_mod.db.conn.commit()

            assert await db_mod.get_subscriptions_needing_reminder(3) == []

        temp_db_loop.run_until_complete(_run())

    def test_unsupported_window_raises(self, temp_db_loop):
        async def _run():
            with pytest.raises(ValueError):
                await db_mod.get_subscriptions_needing_reminder(7)

        temp_db_loop.run_until_complete(_run())

    def test_mark_reminder_sent_persists(self, temp_db_loop):
        async def _run():
            uid = 103
            await db_mod.add_user(uid, "u", "U")
            cur = await db_mod.db.conn.execute(
                "INSERT INTO subscriptions (user_id, plan, tier, expires_at) "
                "VALUES (?, 'monthly', ?, datetime('now', '+2 days'))"
                " RETURNING id",
                (uid, TIER_PRO),
            )
            sub_id = (await cur.fetchone())["id"]
            await db_mod.db.conn.commit()

            await db_mod.mark_reminder_sent(sub_id, 3)

            # And it shouldn't reappear in the reminder query now.
            due = await db_mod.get_subscriptions_needing_reminder(3)
            assert all(r["id"] != sub_id for r in due)

        temp_db_loop.run_until_complete(_run())


class TestUpsellThrottle:
    def test_first_call_allowed(self, temp_db_loop):
        async def _run():
            uid = 200
            await db_mod.add_user(uid, "u", "U")
            cur = await db_mod.db.conn.execute(
                "INSERT INTO subscriptions (user_id, plan, tier, expires_at) "
                "VALUES (?, 'monthly', ?, datetime('now', '+30 days')) RETURNING id",
                (uid, TIER_BASIC),
            )
            sub_id = (await cur.fetchone())["id"]
            await db_mod.db.conn.commit()

            assert await db_mod.can_send_upsell(sub_id) is True

        temp_db_loop.run_until_complete(_run())

    def test_recent_upsell_blocks_subsequent(self, temp_db_loop):
        async def _run():
            uid = 201
            await db_mod.add_user(uid, "u", "U")
            cur = await db_mod.db.conn.execute(
                "INSERT INTO subscriptions (user_id, plan, tier, expires_at) "
                "VALUES (?, 'monthly', ?, datetime('now', '+30 days')) RETURNING id",
                (uid, TIER_BASIC),
            )
            sub_id = (await cur.fetchone())["id"]
            await db_mod.db.conn.commit()

            await db_mod.mark_upsell_sent(sub_id)
            assert await db_mod.can_send_upsell(sub_id) is False
            # But with a tiny throttle window we *should* be allowed again.
            assert await db_mod.can_send_upsell(sub_id, throttle_seconds=0) is True

        temp_db_loop.run_until_complete(_run())


# ── parser zero-results streak alert ────────────────────────────────────────


class _StubParser:
    def __init__(self, name: str, result):
        self.platform_name = name
        self._result = result

    async def safe_parse(self):
        if isinstance(self._result, Exception):
            raise self._result
        return list(self._result)

    async def close(self):
        pass


class TestParserZeroResultsAlert:
    def test_admins_paged_after_streak_threshold(self, temp_db_loop):
        """A parser returning empty results for `PARSER_ZERO_RESULTS_ALERT_STREAK`
        ticks must trigger an admin alert exactly once per streak."""

        async def _run():
            scheduler_mod._parser_zero_streak.clear()
            bot = MagicMock()
            bot.send_message = AsyncMock()
            with patch.object(scheduler_mod.config, "admin_ids", [12345]):
                stub = _StubParser("Kwork", [])
                for _ in range(PARSER_ZERO_RESULTS_ALERT_STREAK - 1):
                    await scheduler_mod.parse_all_platforms([stub], bot=bot)
                # Below threshold: no alerts yet.
                assert bot.send_message.await_count == 0
                # Crossing the threshold fires exactly one alert.
                await scheduler_mod.parse_all_platforms([stub], bot=bot)
                assert bot.send_message.await_count == 1
                # And the streak resets so we don't re-alert next tick.
                assert scheduler_mod._parser_zero_streak["Kwork"] == 0

        temp_db_loop.run_until_complete(_run())

    def test_streak_resets_when_parser_recovers(self, temp_db_loop):
        async def _run():
            scheduler_mod._parser_zero_streak.clear()
            bot = MagicMock()
            bot.send_message = AsyncMock()
            with patch.object(scheduler_mod.config, "admin_ids", [12345]):
                stub_empty = _StubParser("FL.ru", [])
                # Two empty ticks (just below the alert threshold).
                for _ in range(2):
                    await scheduler_mod.parse_all_platforms([stub_empty], bot=bot)
                assert scheduler_mod._parser_zero_streak["FL.ru"] == 2

                # Recovery: a parsed order resets the streak to 0.
                good = SimpleNamespace(to_dict=lambda: _order("FL.ru", "real-1"))
                stub_ok = _StubParser("FL.ru", [good])
                await scheduler_mod.parse_all_platforms([stub_ok], bot=bot)
                assert scheduler_mod._parser_zero_streak["FL.ru"] == 0
                assert bot.send_message.await_count == 0

        temp_db_loop.run_until_complete(_run())


# ── scheduler graceful shutdown ─────────────────────────────────────────────


class TestSchedulerShutdown:
    def test_stop_event_unwinds_loop_promptly(self, temp_db_loop):
        """Setting stop_event must let `scheduler_loop` return cleanly within
        a couple of seconds, not block until the next parse interval."""

        async def _run():
            stop_event = asyncio.Event()
            bot = MagicMock()
            bot.send_message = AsyncMock()

            # Patch the parsers list and parse to no-ops so the test doesn't
            # actually hit the network.
            scheduler_mod._parser_zero_streak.clear()
            with patch.object(scheduler_mod, "_build_parsers", return_value=[]), \
                 patch.object(
                     scheduler_mod, "parse_all_platforms",
                     new=AsyncMock(return_value=0),
                 ), \
                 patch.object(scheduler_mod.config, "parse_interval", 60):

                task = asyncio.create_task(
                    scheduler_mod.scheduler_loop(bot, stop_event=stop_event)
                )
                # Let it run one tick.
                await asyncio.sleep(0.2)
                stop_event.set()
                # It must finish in well under the parse interval (60min).
                await asyncio.wait_for(task, timeout=5.0)

        temp_db_loop.run_until_complete(_run())


# ── /cancel handler ─────────────────────────────────────────────────────────


class TestCancelHandler:
    def test_cancel_clears_state_and_replies(self, temp_db_loop):
        from bot.handlers.start import cmd_cancel

        async def _run():
            msg = MagicMock()
            msg.answer = AsyncMock()
            state = MagicMock()
            state.get_state = AsyncMock(return_value="some-state")
            state.clear = AsyncMock()
            await cmd_cancel(msg, state)
            state.clear.assert_awaited_once()
            msg.answer.assert_awaited_once()
            args, kwargs = msg.answer.await_args
            assert "отменено" in args[0].lower()

        temp_db_loop.run_until_complete(_run())

    def test_cancel_when_no_state_still_replies(self, temp_db_loop):
        from bot.handlers.start import cmd_cancel

        async def _run():
            msg = MagicMock()
            msg.answer = AsyncMock()
            state = MagicMock()
            state.get_state = AsyncMock(return_value=None)
            state.clear = AsyncMock()
            await cmd_cancel(msg, state)
            # Skipping clear() when there's no state is the right behaviour.
            state.clear.assert_not_called()
            msg.answer.assert_awaited_once()

        temp_db_loop.run_until_complete(_run())


# ── /health endpoint ────────────────────────────────────────────────────────


class TestHealthEndpoint:
    def test_health_returns_ok_with_live_db(self, temp_db_loop):
        from bot.payments.webhook import _handle_health

        async def _run():
            request = MagicMock()
            response = await _handle_health(request)
            assert response.status == 200
            body = response.body.decode()
            assert "ok" in body

        temp_db_loop.run_until_complete(_run())

    def test_health_returns_503_when_db_disconnected(self):
        from bot.payments.webhook import _handle_health

        async def _run():
            old = db_mod.db
            db_mod.db = db_mod.Database("/tmp/never-opened.db")
            try:
                request = MagicMock()
                response = await _handle_health(request)
                # Either 503 "starting" (no conn) or 503 "unhealthy" — both
                # explicitly signal "don't route traffic here yet".
                assert response.status == 503
            finally:
                db_mod.db = old

        # We don't use the temp_db_loop fixture here because we want a clean
        # disconnected state.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()
