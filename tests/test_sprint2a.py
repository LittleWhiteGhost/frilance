"""Sprint 2A tests: Telegram Stars, referrals, promo codes.

These tests live alongside test_payment.py / test_sprint1.py and use the
same fixture pattern (a fresh sqlite per test, populated through the public
helpers in bot.database).
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
from bot.constants import (
    PROMO_KIND_BONUS_DAYS,
    PROMO_KIND_DISCOUNT_PCT,
    REFERRAL_INVITED_TRIAL_BONUS_DAYS,
    REFERRAL_REFERRER_BONUS_DAYS,
    TIER_PRO,
)


# ── fixtures ────────────────────────────────────────────────────────────────


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
        path = os.path.join(tmp, "test.db")
        loop = asyncio.new_event_loop()
        try:
            old = loop.run_until_complete(_setup(path))
            yield loop
            loop.run_until_complete(_teardown(old))
        finally:
            loop.close()


def _make_message_mock():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock()
    msg.chat = SimpleNamespace(id=999)
    return msg


def _make_callback(data: str, user_id: int = 42, message=None, bot=None):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id)
    cb.message = message or _make_message_mock()
    cb.answer = AsyncMock()
    cb.bot = bot or MagicMock(send_invoice=AsyncMock())
    return cb


# ── Schema v4/v5 sanity ─────────────────────────────────────────────────────


class TestSchemaMigrations:
    def test_v4_columns_exist(self, temp_db):
        async def _run():
            cur = await db_mod.db.conn.execute("PRAGMA table_info(users)")
            cols = {r["name"] for r in await cur.fetchall()}
            assert "referral_code" in cols
            assert "referred_by" in cols

            cur = await db_mod.db.conn.execute("PRAGMA table_info(payments)")
            cols = {r["name"] for r in await cur.fetchall()}
            assert "provider" in cols

        temp_db.run_until_complete(_run())

    def test_v4_tables_exist(self, temp_db):
        async def _run():
            for table in ("promo_codes", "promo_redemptions", "bonus_days_ledger"):
                cur = await db_mod.db.conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name = ?",
                    (table,),
                )
                assert await cur.fetchone() is not None, f"{table} missing"

        temp_db.run_until_complete(_run())

    def test_v5_events_table_exists(self, temp_db):
        async def _run():
            cur = await db_mod.db.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name = 'events'"
            )
            assert await cur.fetchone() is not None

        temp_db.run_until_complete(_run())

    def test_user_version_is_5(self, temp_db):
        async def _run():
            cur = await db_mod.db.conn.execute("PRAGMA user_version")
            row = await cur.fetchone()
            assert int(row[0]) == 5

        temp_db.run_until_complete(_run())


# ── Referral DB helpers ─────────────────────────────────────────────────────


class TestReferralCode:
    def test_lazy_generation(self, temp_db):
        """Code is NULL until /referral is invoked the first time."""
        async def _run():
            await db_mod.add_user(7, "u", "U")
            cur = await db_mod.db.conn.execute(
                "SELECT referral_code FROM users WHERE user_id = 7"
            )
            assert (await cur.fetchone())["referral_code"] is None

            code = await db_mod.get_or_create_referral_code(7, length=7)
            assert code and len(code) == 7
            # Idempotent: second call returns the same code.
            assert await db_mod.get_or_create_referral_code(7, length=7) == code

        temp_db.run_until_complete(_run())

    def test_find_by_code_round_trip(self, temp_db):
        async def _run():
            await db_mod.add_user(8, "u", "U")
            code = await db_mod.get_or_create_referral_code(8, length=7)
            row = await db_mod.find_user_by_referral_code(code)
            assert row is not None and row["user_id"] == 8
            # Lower-case input should still match (UI is case-tolerant).
            assert (await db_mod.find_user_by_referral_code(code.lower()))[
                "user_id"
            ] == 8
            assert await db_mod.find_user_by_referral_code("NOPE12345") is None

        temp_db.run_until_complete(_run())

    def test_count_referrals(self, temp_db):
        async def _run():
            await db_mod.add_user(1, "a", "A")
            assert await db_mod.count_referrals(1) == 0
            await db_mod.add_user(2, "b", "B", referred_by=1)
            await db_mod.add_user(3, "c", "C", referred_by=1)
            await db_mod.add_user(4, "d", "D", referred_by=2)
            assert await db_mod.count_referrals(1) == 2
            assert await db_mod.count_referrals(2) == 1

        temp_db.run_until_complete(_run())

    def test_self_referral_dropped(self, temp_db):
        """A malicious user can't refer themselves to farm bonus days."""
        async def _run():
            await db_mod.add_user(5, "u", "U", referred_by=5)
            assert await db_mod.get_referrer_id(5) is None

        temp_db.run_until_complete(_run())

    def test_referred_by_only_set_on_first_insert(self, temp_db):
        """Re-running /start with another ref link must NOT change `referred_by`."""
        async def _run():
            await db_mod.add_user(10, "u", "U", referred_by=None)
            await db_mod.add_user(10, "u", "U", referred_by=99)
            assert await db_mod.get_referrer_id(10) is None

        temp_db.run_until_complete(_run())


# ── Bonus-days ledger ───────────────────────────────────────────────────────


class TestBonusDaysLedger:
    def test_credit_then_consume_on_activation(self, temp_db):
        """Bonus days extend `expires_at` on the next activate_subscription call."""
        async def _run():
            await db_mod.add_user(20, "u", "U")
            await db_mod.add_bonus_days(20, 7, "promo", "WELCOME")
            assert await db_mod.get_unconsumed_bonus_days(20) == 7

            await db_mod.activate_subscription(20, TIER_PRO, "monthly", "yk_p_1")

            # Bonus consumed.
            assert await db_mod.get_unconsumed_bonus_days(20) == 0

            sub = await db_mod.get_active_subscription(20)
            assert sub is not None
            expires = datetime.fromisoformat(sub["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            # 30 days (monthly) + 7 (bonus) ± a few hours of slack.
            delta = expires - now
            assert timedelta(days=36) <= delta <= timedelta(days=38)

        temp_db.run_until_complete(_run())

    def test_no_double_spend(self, temp_db):
        """Activating twice doesn't credit bonus days twice."""
        async def _run():
            await db_mod.add_user(21, "u", "U")
            await db_mod.add_bonus_days(21, 7, "promo", "X")
            await db_mod.activate_subscription(21, TIER_PRO, "monthly", "yk_p_a")
            await db_mod.activate_subscription(21, TIER_PRO, "monthly", "yk_p_b")
            assert await db_mod.get_unconsumed_bonus_days(21) == 0

        temp_db.run_until_complete(_run())


# ── Promo code DB helpers ───────────────────────────────────────────────────


class TestPromoCodes:
    def test_create_get(self, temp_db):
        async def _run():
            cid = await db_mod.create_promo_code(
                "WELCOME7", PROMO_KIND_BONUS_DAYS, 7, max_uses=5
            )
            assert cid > 0
            row = await db_mod.get_promo_code("welcome7")
            assert row is not None
            assert row["code"] == "WELCOME7"
            assert row["max_uses"] == 5
            assert row["used_count"] == 0

        temp_db.run_until_complete(_run())

    def test_unique_constraint(self, temp_db):
        async def _run():
            await db_mod.create_promo_code(
                "DUP", PROMO_KIND_BONUS_DAYS, 3
            )
            with pytest.raises(Exception):
                await db_mod.create_promo_code(
                    "DUP", PROMO_KIND_BONUS_DAYS, 3
                )

        temp_db.run_until_complete(_run())

    def test_redeem_max_uses_enforced(self, temp_db):
        async def _run():
            cid = await db_mod.create_promo_code(
                "TWICE", PROMO_KIND_BONUS_DAYS, 1, max_uses=2
            )
            await db_mod.add_user(1, "a", "A")
            await db_mod.add_user(2, "b", "B")
            await db_mod.add_user(3, "c", "C")
            assert await db_mod.redeem_promo_code(cid, 1) is True
            assert await db_mod.redeem_promo_code(cid, 2) is True
            # Third attempt: max_uses exhausted.
            assert await db_mod.redeem_promo_code(cid, 3) is False

        temp_db.run_until_complete(_run())

    def test_redeem_one_per_user(self, temp_db):
        async def _run():
            cid = await db_mod.create_promo_code(
                "ONCE", PROMO_KIND_BONUS_DAYS, 5, max_uses=10
            )
            await db_mod.add_user(7, "u", "U")
            assert await db_mod.redeem_promo_code(cid, 7) is True
            # Second redemption by same user is rejected.
            assert await db_mod.redeem_promo_code(cid, 7) is False
            row = await db_mod.get_promo_code("ONCE")
            assert row["used_count"] == 1

        temp_db.run_until_complete(_run())

    def test_expired_code_rejected(self, temp_db):
        async def _run():
            cid = await db_mod.create_promo_code(
                "EXPIRED", PROMO_KIND_BONUS_DAYS, 5,
                expires_at="2020-01-01 00:00:00",
            )
            await db_mod.add_user(8, "u", "U")
            assert await db_mod.redeem_promo_code(cid, 8) is False

        temp_db.run_until_complete(_run())


# ── /promo handler ──────────────────────────────────────────────────────────


def _make_message(text: str, user_id: int = 50):
    m = MagicMock()
    m.from_user = SimpleNamespace(id=user_id, username="u", full_name="U")
    m.text = text
    m.answer = AsyncMock()
    return m


class TestPromoHandler:
    def test_bonus_days_credited_immediately(self, temp_db):
        from aiogram.filters import CommandObject
        from bot.handlers.promo import cmd_promo

        async def _run():
            await db_mod.add_user(50, "u", "U")
            await db_mod.create_promo_code(
                "GIFT3", PROMO_KIND_BONUS_DAYS, 3, max_uses=10
            )
            msg = _make_message("/promo GIFT3", user_id=50)
            await cmd_promo(msg, CommandObject(prefix="/", command="promo", args="GIFT3"))
            assert await db_mod.get_unconsumed_bonus_days(50) == 3
            msg.answer.assert_called_once()
            (call_args, _) = msg.answer.call_args
            assert "+3 дней" in call_args[0]

        temp_db.run_until_complete(_run())

    def test_double_redemption_blocked(self, temp_db):
        from aiogram.filters import CommandObject
        from bot.handlers.promo import cmd_promo

        async def _run():
            await db_mod.add_user(60, "u", "U")
            await db_mod.create_promo_code(
                "ONCE2", PROMO_KIND_BONUS_DAYS, 2
            )
            args = CommandObject(prefix="/", command="promo", args="ONCE2")
            await cmd_promo(_make_message("/promo ONCE2", 60), args)
            assert await db_mod.get_unconsumed_bonus_days(60) == 2

            msg2 = _make_message("/promo ONCE2", 60)
            await cmd_promo(msg2, args)
            # Bonus stays at 2 (no double credit).
            assert await db_mod.get_unconsumed_bonus_days(60) == 2

        temp_db.run_until_complete(_run())

    def test_unknown_code_silently_rejected(self, temp_db):
        from aiogram.filters import CommandObject
        from bot.handlers.promo import cmd_promo

        async def _run():
            await db_mod.add_user(70, "u", "U")
            msg = _make_message("/promo NOSUCH", 70)
            await cmd_promo(
                msg,
                CommandObject(prefix="/", command="promo", args="NOSUCH"),
            )
            (call_args, _) = msg.answer.call_args
            # Generic "didn't match" message — must NOT reveal "code not found".
            assert "не подошёл" in call_args[0].lower() or \
                   "не подош" in call_args[0]

        temp_db.run_until_complete(_run())

    def test_missing_arg_shows_usage(self, temp_db):
        from aiogram.filters import CommandObject
        from bot.handlers.promo import cmd_promo

        async def _run():
            await db_mod.add_user(80, "u", "U")
            msg = _make_message("/promo", 80)
            await cmd_promo(
                msg,
                CommandObject(prefix="/", command="promo", args=None),
            )
            (call_args, _) = msg.answer.call_args
            assert "/promo" in call_args[0]

        temp_db.run_until_complete(_run())


# ── /referral handler ───────────────────────────────────────────────────────


class TestReferralHandler:
    def test_referral_link_shown(self, temp_db):
        from aiogram.filters import Command  # noqa: F401
        from bot.config import config as cfg
        from bot.handlers.start import cmd_referral

        async def _run():
            cfg.bot_username = "MyTestBot"
            await db_mod.add_user(100, "u", "U")
            msg = _make_message("/referral", 100)
            await cmd_referral(msg)
            (call_args, _) = msg.answer.call_args
            text = call_args[0]
            assert "https://t.me/MyTestBot?start=ref_" in text
            assert "Приглашено:</b> 0" in text
            # Code is now persisted on the user.
            cur = await db_mod.db.conn.execute(
                "SELECT referral_code FROM users WHERE user_id = 100"
            )
            assert (await cur.fetchone())["referral_code"] is not None

        temp_db.run_until_complete(_run())


# ── /start with referral payload ────────────────────────────────────────────


class TestStartWithReferralPayload:
    def test_invited_user_gets_trial_bonus_and_referrer_credited(self, temp_db):
        from bot.handlers.start import cmd_start

        async def _run():
            # Step 1: referrer registers.
            inviter_id = 200
            invitee_id = 201
            inviter_msg = _make_message("/start", inviter_id)
            inviter_msg.from_user = SimpleNamespace(
                id=inviter_id, username="inv", full_name="Inv"
            )
            await cmd_start(inviter_msg)
            inviter_code = await db_mod.get_or_create_referral_code(
                inviter_id, length=7
            )

            # Step 2: invitee /start ref_<code>.
            invitee_msg = _make_message(f"/start ref_{inviter_code}", invitee_id)
            invitee_msg.from_user = SimpleNamespace(
                id=invitee_id, username="inv2", full_name="Inv2"
            )
            await cmd_start(invitee_msg)

            # Invitee's trial got a bonus.
            sub = await db_mod.get_active_subscription(invitee_id)
            assert sub is not None
            expires = datetime.fromisoformat(sub["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = expires - now
            from bot.config import config as cfg
            expected_days = cfg.trial_days + REFERRAL_INVITED_TRIAL_BONUS_DAYS
            assert timedelta(days=expected_days - 1) <= delta <= \
                   timedelta(days=expected_days + 1)

            # Referrer was credited.
            assert await db_mod.get_unconsumed_bonus_days(inviter_id) == \
                REFERRAL_REFERRER_BONUS_DAYS
            # Link is persisted.
            assert await db_mod.get_referrer_id(invitee_id) == inviter_id

        temp_db.run_until_complete(_run())

    def test_unknown_ref_code_does_not_block_signup(self, temp_db):
        from bot.handlers.start import cmd_start

        async def _run():
            user_id = 210
            msg = _make_message("/start ref_BOGUS00", user_id)
            msg.from_user = SimpleNamespace(
                id=user_id, username="u", full_name="U"
            )
            await cmd_start(msg)
            # User still got created with a clean trial.
            sub = await db_mod.get_active_subscription(user_id)
            assert sub is not None
            assert await db_mod.get_referrer_id(user_id) is None

        temp_db.run_until_complete(_run())


# ── Telegram Stars flow ─────────────────────────────────────────────────────


class TestStarsHandlers:
    def test_pay_callback_shows_method_picker(self, temp_db):
        from bot.handlers.payment import cb_pay

        async def _run():
            await db_mod.add_user(300, "u", "U")
            cb = _make_callback("pay:pro:monthly", user_id=300)
            await cb_pay(cb)
            cb.message.edit_text.assert_called_once()
            (args, kwargs) = cb.message.edit_text.call_args
            text = args[0] if args else kwargs.get("text", "")
            assert "способ оплаты" in text.lower() or "способ оплаты" in text

        temp_db.run_until_complete(_run())

    def test_paym_xtr_sends_invoice(self, temp_db):
        from bot.handlers.payment import cb_payment_method

        async def _run():
            await db_mod.add_user(310, "u", "U")
            send_invoice = AsyncMock()
            bot = MagicMock(send_invoice=send_invoice)
            cb = _make_callback(
                "paym:xtr:pro:monthly", user_id=310, bot=bot,
            )
            await cb_payment_method(cb)
            send_invoice.assert_called_once()
            kwargs = send_invoice.call_args.kwargs
            assert kwargs["currency"] == "XTR"
            assert kwargs["chat_id"] == 999
            # Payload is a parseable round-trip.
            from bot.handlers.payment import _parse_stars_payload
            parsed = _parse_stars_payload(kwargs["payload"])
            assert parsed == (310, "pro", "monthly")

        temp_db.run_until_complete(_run())

    def test_pre_checkout_validates_amount(self, temp_db):
        from bot.config import config as cfg
        from bot.handlers.payment import cb_pre_checkout

        async def _run():
            q = MagicMock()
            q.from_user = SimpleNamespace(id=400)
            q.invoice_payload = "sub:400:pro:monthly:abcdef"
            q.currency = "XTR"
            q.total_amount = cfg.stars_price_for(TIER_PRO, "monthly")
            q.answer = AsyncMock()

            await cb_pre_checkout(q)
            q.answer.assert_called_once()
            kwargs = q.answer.call_args.kwargs
            assert kwargs.get("ok") is True

        temp_db.run_until_complete(_run())

    def test_pre_checkout_rejects_amount_mismatch(self, temp_db):
        from bot.handlers.payment import cb_pre_checkout

        async def _run():
            q = MagicMock()
            q.from_user = SimpleNamespace(id=401)
            q.invoice_payload = "sub:401:pro:monthly:abcdef"
            q.currency = "XTR"
            q.total_amount = 1  # bogus
            q.answer = AsyncMock()

            await cb_pre_checkout(q)
            kwargs = q.answer.call_args.kwargs
            assert kwargs.get("ok") is False
            assert "сум" in kwargs.get("error_message", "").lower()

        temp_db.run_until_complete(_run())

    def test_pre_checkout_rejects_user_mismatch(self, temp_db):
        from bot.config import config as cfg
        from bot.handlers.payment import cb_pre_checkout

        async def _run():
            q = MagicMock()
            q.from_user = SimpleNamespace(id=999)
            q.invoice_payload = "sub:402:pro:monthly:abcdef"
            q.currency = "XTR"
            q.total_amount = cfg.stars_price_for(TIER_PRO, "monthly")
            q.answer = AsyncMock()

            await cb_pre_checkout(q)
            kwargs = q.answer.call_args.kwargs
            assert kwargs.get("ok") is False

        temp_db.run_until_complete(_run())

    def test_successful_payment_activates_subscription(self, temp_db):
        from bot.config import config as cfg
        from bot.handlers.payment import cb_successful_payment

        async def _run():
            user_id = 500
            await db_mod.add_user(user_id, "u", "U")

            msg = MagicMock()
            msg.from_user = SimpleNamespace(id=user_id)
            msg.answer = AsyncMock()
            msg.successful_payment = SimpleNamespace(
                invoice_payload=f"sub:{user_id}:pro:monthly:nonce123",
                total_amount=cfg.stars_price_for(TIER_PRO, "monthly"),
                telegram_payment_charge_id="tg_charge_xyz",
            )

            await cb_successful_payment(msg)

            sub = await db_mod.get_active_subscription(user_id)
            assert sub is not None
            assert sub["tier"] == TIER_PRO
            assert sub["plan"] == "monthly"

            payment = await db_mod.get_payment("stars-tg_charge_xyz")
            assert payment is not None
            assert payment["status"] == "succeeded"
            assert payment["provider"] == "stars"
            assert payment["currency"] == "XTR"

        temp_db.run_until_complete(_run())

    def test_successful_payment_user_mismatch_does_not_activate(self, temp_db):
        from bot.handlers.payment import cb_successful_payment

        async def _run():
            user_id = 600
            await db_mod.add_user(user_id, "u", "U")
            msg = MagicMock()
            msg.from_user = SimpleNamespace(id=user_id)
            msg.answer = AsyncMock()
            msg.successful_payment = SimpleNamespace(
                invoice_payload="sub:999:pro:monthly:nonce",
                total_amount=500,
                telegram_payment_charge_id="charge_attack",
            )
            await cb_successful_payment(msg)
            assert await db_mod.get_active_subscription(user_id) is None

        temp_db.run_until_complete(_run())


# ── Event logging ────────────────────────────────────────────────────────


class TestEventLog:
    def test_log_event_persists(self, temp_db):
        async def _run():
            await db_mod.log_event(
                "test_event", user_id=42, properties={"k": "v"},
            )
            cur = await db_mod.db.conn.execute(
                "SELECT * FROM events WHERE event_type = 'test_event'"
            )
            row = await cur.fetchone()
            assert row is not None
            assert row["user_id"] == 42
            assert '"k"' in row["properties"]

        temp_db.run_until_complete(_run())

    def test_log_event_handles_unserializable_properties(self, temp_db):
        async def _run():
            class Weird:
                def __repr__(self):
                    return "Weird()"

            await db_mod.log_event(
                "weird", user_id=1, properties={"obj": Weird()},
            )
            cur = await db_mod.db.conn.execute(
                "SELECT properties FROM events WHERE event_type = 'weird'"
            )
            row = await cur.fetchone()
            assert row is not None
            assert "Weird" in row["properties"]

        temp_db.run_until_complete(_run())
