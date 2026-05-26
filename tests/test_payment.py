"""Tests for the payment handler, webhook amount-validation, and the
idempotency / authorization rules in bot.handlers.payment.

The handler is the most security-critical module: a missed check here means a
user can activate a tier they didn't pay for. These tests pin down the
non-negotiable invariants.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import database as db_mod
from bot.config import config
from bot.constants import TIER_BASIC, TIER_MAX, TIER_PRO, TIERS
from bot.handlers.payment import (
    _amount_matches,
    _parse_pay_callback,
    cb_check_payment,
    cb_pay,
)
from bot.payments.webhook import _amount_matches as webhook_amount_matches


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """A fresh temp DB for each test that re-points the global `db` singleton.
    Cleanly torn down after the test regardless of failures."""
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
    """A CallbackQuery.message stub with awaitable edit_text()."""
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    return msg


def _make_callback(data: str, user_id: int = 42, message=None):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id)
    cb.message = message or _make_message_mock()
    cb.answer = AsyncMock()
    return cb


# ── _parse_pay_callback ─────────────────────────────────────────────────────


class TestParsePayCallback:
    def test_three_segment_form_required(self):
        # The handler must reject 2-segment `pay:<plan>` forms outright,
        # otherwise an attacker could replay an old button to bypass tier check.
        assert _parse_pay_callback("pay:monthly") is None
        assert _parse_pay_callback("pay:yearly") is None

    def test_unknown_first_segment_rejected(self):
        # Anything that isn't literally `pay:` must be refused.
        assert _parse_pay_callback("buy:basic:monthly") is None
        assert _parse_pay_callback("Pay:basic:monthly") is None  # case-sensitive

    @pytest.mark.parametrize("tier", TIERS)
    @pytest.mark.parametrize("plan", ["monthly", "yearly"])
    def test_canonical_form_accepted(self, tier, plan):
        assert _parse_pay_callback(f"pay:{tier}:{plan}") == (tier, plan)

    def test_extra_segments_rejected(self):
        # No fuzz-room for "pay:basic:monthly:gift" smuggling.
        assert _parse_pay_callback("pay:basic:monthly:extra") is None

    def test_empty_segments_rejected(self):
        assert _parse_pay_callback("pay::monthly") is None
        assert _parse_pay_callback("pay:basic:") is None

    def test_unknown_tier_rejected(self):
        assert _parse_pay_callback("pay:ultra:monthly") is None
        assert _parse_pay_callback("pay:vip:yearly") is None

    def test_unknown_plan_rejected(self):
        assert _parse_pay_callback("pay:basic:lifetime") is None
        assert _parse_pay_callback("pay:pro:weekly") is None


# ── _amount_matches ─────────────────────────────────────────────────────────


class TestAmountMatches:
    def test_exact_match(self):
        # Callback path must accept the exact configured price.
        assert _amount_matches(
            str(config.price_for(TIER_PRO, "monthly")), TIER_PRO, "monthly"
        )

    def test_string_decimal_format(self):
        # YooKassa returns "599.00", not 599. Accept both representations.
        price = config.price_for(TIER_PRO, "monthly")
        assert _amount_matches(f"{price:.2f}", TIER_PRO, "monthly")
        assert _amount_matches(str(price), TIER_PRO, "monthly")
        assert _amount_matches(Decimal(price), TIER_PRO, "monthly")

    def test_underpayment_rejected(self):
        # Critical: paying less than the price must not activate the tier.
        price = config.price_for(TIER_MAX, "monthly")
        assert not _amount_matches(str(price - 1), TIER_MAX, "monthly")
        assert not _amount_matches("0.01", TIER_MAX, "monthly")

    def test_wrong_tier_rejected(self):
        # Paying Basic price for Max activation must fail.
        basic_price = config.price_for(TIER_BASIC, "monthly")
        assert not _amount_matches(str(basic_price), TIER_MAX, "monthly")

    def test_wrong_plan_rejected(self):
        # Paying monthly price for yearly activation must fail.
        monthly = config.price_for(TIER_PRO, "monthly")
        assert not _amount_matches(str(monthly), TIER_PRO, "yearly")

    def test_garbage_value_rejected(self):
        # Defensive: non-numeric input from YooKassa must NOT crash and must NOT match.
        assert not _amount_matches("not-a-number", TIER_PRO, "monthly")
        assert not _amount_matches(None, TIER_PRO, "monthly")
        assert not _amount_matches(object(), TIER_PRO, "monthly")

    def test_webhook_uses_same_validator(self):
        # Both call sites (cb_check_payment and webhook) must agree.
        price = config.price_for(TIER_PRO, "monthly")
        assert webhook_amount_matches(str(price), TIER_PRO, "monthly")
        assert not webhook_amount_matches(str(price - 1), TIER_PRO, "monthly")


# ── cb_check_payment authorization ──────────────────────────────────────────


class TestCheckPaymentAuthorization:
    def test_payment_belonging_to_other_user_rejected(self, temp_db):
        """A user cannot claim someone else's payment id by guessing it."""

        async def _run():
            owner_id = 1001
            attacker_id = 2002
            payment_id = "yk_owner_payment_123"
            price = config.price_for(TIER_PRO, "monthly")

            # Both users must exist (payments has a FK to users).
            await db_mod.add_user(owner_id, "owner", "Owner")
            await db_mod.add_user(attacker_id, "att", "Attacker")
            # Owner's pending payment exists in DB.
            await db_mod.save_payment(
                owner_id, payment_id, float(price), TIER_PRO, "monthly"
            )

            cb = _make_callback(f"check_pay:{payment_id}", user_id=attacker_id)

            # Even if YooKassa would say "succeeded", the handler must reject
            # before calling the SDK because user_id doesn't match.
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value={
                    "payment_id": payment_id,
                    "status": "succeeded",
                    "paid": True,
                    "amount": str(price),
                    "metadata": {},
                }),
            ):
                await cb_check_payment(cb)

            cb.answer.assert_called()
            # Subscription must NOT be activated for the attacker.
            sub = await db_mod.get_active_subscription(attacker_id)
            assert sub is None

        temp_db.run_until_complete(_run())

    def test_succeeded_payment_activates_subscription(self, temp_db):
        async def _run():
            user_id = 3003
            payment_id = "yk_pro_monthly_555"
            price = config.price_for(TIER_PRO, "monthly")
            await db_mod.add_user(user_id, "u", "Test User")
            await db_mod.save_payment(
                user_id, payment_id, float(price), TIER_PRO, "monthly"
            )

            cb = _make_callback(f"check_pay:{payment_id}", user_id=user_id)
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value={
                    "payment_id": payment_id,
                    "status": "succeeded",
                    "paid": True,
                    "amount": str(price),
                    "metadata": {},
                }),
            ):
                await cb_check_payment(cb)

            sub = await db_mod.get_active_subscription(user_id)
            assert sub is not None
            assert sub["tier"] == TIER_PRO
            assert sub["plan"] == "monthly"
            assert sub["payment_id"] == payment_id

        temp_db.run_until_complete(_run())

    def test_amount_mismatch_does_not_activate(self, temp_db):
        """Paying Basic price (299) but trying to activate Pro must be rejected
        even though the YooKassa payment is in 'succeeded' state."""

        async def _run():
            user_id = 4004
            payment_id = "yk_underpay_777"
            await db_mod.add_user(user_id, "u", "U")
            # Stored as Pro / monthly in our DB...
            await db_mod.save_payment(
                user_id, payment_id,
                float(config.price_for(TIER_PRO, "monthly")),
                TIER_PRO, "monthly",
            )

            cb = _make_callback(f"check_pay:{payment_id}", user_id=user_id)
            # ...but YooKassa reports the user only paid 1 ₽.
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value={
                    "payment_id": payment_id,
                    "status": "succeeded",
                    "paid": True,
                    "amount": "1.00",
                    "metadata": {},
                }),
            ):
                await cb_check_payment(cb)

            sub = await db_mod.get_active_subscription(user_id)
            assert sub is None, "Pro must NOT activate on a 1₽ payment"

        temp_db.run_until_complete(_run())

    def test_double_check_is_idempotent(self, temp_db):
        """Pressing 'I paid' twice must not extend the subscription twice."""

        async def _run():
            user_id = 5005
            payment_id = "yk_idem_999"
            price = config.price_for(TIER_BASIC, "monthly")
            await db_mod.add_user(user_id, "u", "U")
            await db_mod.save_payment(
                user_id, payment_id, float(price), TIER_BASIC, "monthly"
            )

            yk_resp = {
                "payment_id": payment_id,
                "status": "succeeded",
                "paid": True,
                "amount": str(price),
                "metadata": {},
            }

            cb1 = _make_callback(f"check_pay:{payment_id}", user_id=user_id)
            cb2 = _make_callback(f"check_pay:{payment_id}", user_id=user_id)
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value=yk_resp),
            ):
                await cb_check_payment(cb1)
                # Capture expiry after first activation.
                sub_after_first = await db_mod.get_active_subscription(user_id)
                assert sub_after_first is not None
                first_expires = sub_after_first["expires_at"]

                # Spam-click: must be a no-op (don't extend expiry).
                await cb_check_payment(cb2)
                sub_after_second = await db_mod.get_active_subscription(user_id)
                assert sub_after_second is not None
                assert sub_after_second["expires_at"] == first_expires, (
                    "second click must not extend expiry"
                )

            # No DUPLICATE active subscription rows for the same user.
            cur = await db_mod.db.conn.execute(
                "SELECT COUNT(*) AS cnt FROM subscriptions "
                "WHERE user_id = ? AND is_active = 1",
                (user_id,),
            )
            row = await cur.fetchone()
            assert row["cnt"] == 1, "must keep at most one active subscription row"
            # And the payment itself should be marked succeeded.
            payment = await db_mod.get_payment(payment_id)
            assert payment["status"] == "succeeded"
            # Sanity: first activation actually happened.
            assert first_expires is not None

        temp_db.run_until_complete(_run())

    def test_pending_payment_does_not_activate(self, temp_db):
        async def _run():
            user_id = 6006
            payment_id = "yk_pending_111"
            await db_mod.add_user(user_id, "u", "U")
            await db_mod.save_payment(
                user_id, payment_id,
                float(config.price_for(TIER_BASIC, "monthly")),
                TIER_BASIC, "monthly",
            )

            cb = _make_callback(f"check_pay:{payment_id}", user_id=user_id)
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value={
                    "payment_id": payment_id,
                    "status": "pending",
                    "paid": False,
                    "amount": str(config.price_for(TIER_BASIC, "monthly")),
                    "metadata": {},
                }),
            ):
                # Patch sleep so the retry loop doesn't actually wait 4s.
                with patch("bot.handlers.payment.asyncio.sleep", new=AsyncMock()):
                    await cb_check_payment(cb)

            sub = await db_mod.get_active_subscription(user_id)
            assert sub is None

        temp_db.run_until_complete(_run())

    def test_unknown_payment_id_rejected(self, temp_db):
        async def _run():
            user_id = 7007
            cb = _make_callback("check_pay:does-not-exist", user_id=user_id)
            await cb_check_payment(cb)
            sub = await db_mod.get_active_subscription(user_id)
            assert sub is None

        temp_db.run_until_complete(_run())


# ── cb_pay flow ─────────────────────────────────────────────────────────────


class TestCbPay:
    def test_invalid_callback_data_rejected_without_db_write(self, temp_db):
        async def _run():
            cb = _make_callback("pay:ultra:monthly", user_id=8008)
            await cb_pay(cb)
            cb.answer.assert_called()
            # No payment row should have been created.
            cur = await db_mod.db.conn.execute("SELECT COUNT(*) AS cnt FROM payments")
            row = await cur.fetchone()
            assert row["cnt"] == 0

        temp_db.run_until_complete(_run())

    def test_existing_active_paid_subscription_blocks_repurchase(self, temp_db):
        async def _run():
            user_id = 9009
            await db_mod.add_user(user_id, "u", "U")
            # Activate a paid Basic subscription first.
            await db_mod.activate_subscription(user_id, TIER_BASIC, "monthly", "yk_existing_1")

            cb = _make_callback("pay:pro:monthly", user_id=user_id)
            with patch(
                "bot.handlers.payment.create_payment", new=MagicMock(),
            ) as mocked_create:
                await cb_pay(cb)
                # Should not have called create_payment because the user
                # already has an active paid subscription.
                mocked_create.assert_not_called()

        temp_db.run_until_complete(_run())

    def test_trial_user_can_upgrade(self, temp_db):
        """Trial users *must* be allowed to buy a paid subscription
        (otherwise nobody could ever upgrade).

        Sprint 2A: the YooKassa flow now goes through a method picker step
        first (`pay:` shows picker, `paym:rub:` actually calls YooKassa)."""
        from bot.handlers.payment import cb_payment_method

        async def _run():
            user_id = 10010
            await db_mod.add_user(user_id, "u", "U")
            await db_mod.create_trial(user_id)

            cb = _make_callback("pay:pro:monthly", user_id=user_id)
            mocked = MagicMock(return_value={
                "payment_id": "yk_pro_for_trial",
                "confirmation_url": "https://example/pay",
                "amount": config.price_for(TIER_PRO, "monthly"),
            })
            with patch("bot.handlers.payment.create_payment", new=mocked):
                # Step 1: tier+plan picked → method picker is shown, no
                # create_payment call yet.
                await cb_pay(cb)
                mocked.assert_not_called()

                # Step 2: user picks ЮKassa → create_payment fires.
                cb2 = _make_callback("paym:rub:pro:monthly", user_id=user_id)
                await cb_payment_method(cb2)
                mocked.assert_called_once()
                # Positional args identify the (user, tier, plan) triple.
                # Optional kwargs (amount_override, extra_metadata) are
                # implementation details of the discount flow and not pinned
                # here.
                args, kwargs = mocked.call_args
                assert args == (user_id, TIER_PRO, "monthly")

            payment = await db_mod.get_payment("yk_pro_for_trial")
            assert payment is not None
            assert payment["user_id"] == user_id
            assert payment["tier"] == TIER_PRO
            assert payment["provider"] == "yookassa"

        temp_db.run_until_complete(_run())

    def test_yookassa_failure_is_user_facing(self, temp_db):
        """If YooKassa rejects the create call, the user must see a friendly
        error and we must not leave a dangling payment row."""
        from bot.handlers.payment import cb_payment_method

        async def _run():
            user_id = 11011
            await db_mod.add_user(user_id, "u", "U")

            cb = _make_callback("pay:pro:monthly", user_id=user_id)
            with patch(
                "bot.handlers.payment.create_payment",
                new=MagicMock(return_value=None),
            ):
                await cb_pay(cb)
                # Stars-enabled flow shows picker first; the YooKassa branch
                # is what we actually want to test here.
                cb2 = _make_callback("paym:rub:pro:monthly", user_id=user_id)
                await cb_payment_method(cb2)

            cur = await db_mod.db.conn.execute("SELECT COUNT(*) AS cnt FROM payments")
            row = await cur.fetchone()
            assert row["cnt"] == 0

        temp_db.run_until_complete(_run())
