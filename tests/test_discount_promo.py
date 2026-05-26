"""Tests for the discount_pct promo flow end-to-end.

Covers:
* `get_pending_discount_for_user` only returns unconsumed redemptions and
  ignores bonus_days / expired / consumed rows.
* `_start_yookassa` applies the discount, charges the discounted amount, and
  stamps the redemption row with the payment_id (consume on invoice creation).
* `cb_check_payment` validates against the *stored* amount (which is the
  discounted price), not against the catalogue price.
* `get_promo_code` hides expired codes from the lookup.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import database as db_mod
from bot.config import config
from bot.constants import (
    PROMO_KIND_BONUS_DAYS,
    PROMO_KIND_DISCOUNT_PCT,
    TIER_PRO,
)
from bot.handlers.payment import _apply_discount, _start_yookassa, cb_check_payment


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
        path = os.path.join(tmp, "discounts.db")
        loop = asyncio.new_event_loop()
        try:
            old = loop.run_until_complete(_setup(path))
            yield loop
            loop.run_until_complete(_teardown(old))
        finally:
            loop.close()


def _make_callback(data: str, user_id: int):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id)
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock()
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


# ── _apply_discount math ────────────────────────────────────────────────────


class TestApplyDiscount:
    def test_basic_percent(self):
        assert _apply_discount(1000, 25) == 750
        assert _apply_discount(599, 10) == 539  # 599 * 90 // 100 = 539
        assert _apply_discount(299, 50) == 149  # 299 * 50 // 100 = 149

    def test_zero_percent_no_change(self):
        assert _apply_discount(500, 0) == 500

    def test_caps_to_one_ruble(self):
        # 100% off would be 0₽; YooKassa rejects 0-amount invoices, so the
        # helper bottoms out at 1.
        assert _apply_discount(599, 100) == 1
        # A weirdly-large pct also can't take amount below 1.
        assert _apply_discount(599, 9999) == 1

    def test_clamps_negative_pct(self):
        # Defensive: a buggy admin promo with a negative value can't ever
        # *raise* the price above the catalogue.
        assert _apply_discount(500, -10) == 500


# ── get_pending_discount_for_user ──────────────────────────────────────────


class TestPendingDiscountQuery:
    def test_returns_unconsumed_discount_pct(self, temp_db):
        async def _run():
            await db_mod.add_user(1, "u", "U")
            cid = await db_mod.create_promo_code(
                "SAVE20", PROMO_KIND_DISCOUNT_PCT, 20, max_uses=100,
            )
            assert await db_mod.redeem_promo_code(cid, 1) is True
            pending = await db_mod.get_pending_discount_for_user(1)
            assert pending is not None
            assert pending["code"] == "SAVE20"
            assert pending["pct"] == 20

        temp_db.run_until_complete(_run())

    def test_returns_none_for_bonus_days_redemption(self, temp_db):
        async def _run():
            await db_mod.add_user(2, "u", "U")
            cid = await db_mod.create_promo_code(
                "GIFT5", PROMO_KIND_BONUS_DAYS, 5,
            )
            assert await db_mod.redeem_promo_code(cid, 2) is True
            # Bonus-days codes are not "discounts" — they don't reduce the
            # invoice amount, they extend `expires_at`.
            assert await db_mod.get_pending_discount_for_user(2) is None

        temp_db.run_until_complete(_run())

    def test_consumed_redemption_not_returned(self, temp_db):
        async def _run():
            await db_mod.add_user(3, "u", "U")
            cid = await db_mod.create_promo_code(
                "USED", PROMO_KIND_DISCOUNT_PCT, 30,
            )
            await db_mod.redeem_promo_code(cid, 3)
            pending = await db_mod.get_pending_discount_for_user(3)
            assert pending is not None
            await db_mod.mark_discount_redemption_consumed(
                int(pending["redemption_id"]), "yk_test_1",
            )
            # After consumption, no longer pending.
            assert await db_mod.get_pending_discount_for_user(3) is None

        temp_db.run_until_complete(_run())

    def test_expired_code_not_returned_even_if_redemption_pending(self, temp_db):
        async def _run():
            await db_mod.add_user(4, "u", "U")
            cid = await db_mod.create_promo_code(
                "OLDIE", PROMO_KIND_DISCOUNT_PCT, 50,
                expires_at="2099-01-01 00:00:00",
            )
            # Redeem while the code is still valid.
            assert await db_mod.redeem_promo_code(cid, 4) is True
            # Manually backdate the code's expiry to simulate it going stale
            # between redemption and checkout.
            await db_mod.db.conn.execute(
                "UPDATE promo_codes SET expires_at = '2020-01-01' WHERE id = ?",
                (cid,),
            )
            await db_mod.db.conn.commit()
            assert await db_mod.get_pending_discount_for_user(4) is None

        temp_db.run_until_complete(_run())


# ── _start_yookassa discount integration ────────────────────────────────────


class TestStartYookassaDiscount:
    def test_no_discount_charges_catalogue_price(self, temp_db):
        async def _run():
            user_id = 10
            await db_mod.add_user(user_id, "u", "U")
            catalogue = config.price_for(TIER_PRO, "monthly")
            mocked_create = MagicMock(return_value={
                "payment_id": "yk_nodisc",
                "confirmation_url": "https://example/pay",
                "amount": catalogue,
            })
            cb = _make_callback("paym:rub:pro:monthly", user_id=user_id)
            with patch("bot.handlers.payment.create_payment", new=mocked_create):
                await _start_yookassa(cb, TIER_PRO, "monthly")
            args, kwargs = mocked_create.call_args
            assert kwargs.get("amount_override") == catalogue
            assert kwargs.get("extra_metadata") is None
            saved = await db_mod.get_payment("yk_nodisc")
            assert saved is not None
            assert int(saved["amount"]) == catalogue

        temp_db.run_until_complete(_run())

    def test_pending_discount_applied_and_consumed(self, temp_db):
        async def _run():
            user_id = 11
            await db_mod.add_user(user_id, "u", "U")
            cid = await db_mod.create_promo_code(
                "HALF", PROMO_KIND_DISCOUNT_PCT, 50,
            )
            await db_mod.redeem_promo_code(cid, user_id)

            catalogue = config.price_for(TIER_PRO, "monthly")
            expected_discounted = _apply_discount(catalogue, 50)

            mocked_create = MagicMock(return_value={
                "payment_id": "yk_disc",
                "confirmation_url": "https://example/pay",
                "amount": expected_discounted,
            })
            cb = _make_callback("paym:rub:pro:monthly", user_id=user_id)
            with patch("bot.handlers.payment.create_payment", new=mocked_create):
                await _start_yookassa(cb, TIER_PRO, "monthly")

            # 1) discount was applied at create_payment call site
            args, kwargs = mocked_create.call_args
            assert kwargs.get("amount_override") == expected_discounted
            md = kwargs.get("extra_metadata") or {}
            assert md.get("promo_code") == "HALF"
            assert md.get("discount_pct") == "50"

            # 2) payment row stores the discounted amount, not the catalogue
            saved = await db_mod.get_payment("yk_disc")
            assert saved is not None
            assert int(saved["amount"]) == expected_discounted

            # 3) redemption is now consumed (no longer pending)
            assert await db_mod.get_pending_discount_for_user(user_id) is None

        temp_db.run_until_complete(_run())

    def test_second_checkout_does_not_reapply_discount(self, temp_db):
        async def _run():
            user_id = 12
            await db_mod.add_user(user_id, "u", "U")
            cid = await db_mod.create_promo_code(
                "ONCE40", PROMO_KIND_DISCOUNT_PCT, 40,
            )
            await db_mod.redeem_promo_code(cid, user_id)

            catalogue = config.price_for(TIER_PRO, "monthly")
            mocked_create = MagicMock(side_effect=[
                {
                    "payment_id": "yk_first",
                    "confirmation_url": "https://example/pay",
                    "amount": _apply_discount(catalogue, 40),
                },
                {
                    "payment_id": "yk_second",
                    "confirmation_url": "https://example/pay2",
                    "amount": catalogue,
                },
            ])
            with patch("bot.handlers.payment.create_payment", new=mocked_create):
                cb1 = _make_callback("paym:rub:pro:monthly", user_id=user_id)
                await _start_yookassa(cb1, TIER_PRO, "monthly")
                cb2 = _make_callback("paym:rub:pro:monthly", user_id=user_id)
                await _start_yookassa(cb2, TIER_PRO, "monthly")

            calls = mocked_create.call_args_list
            # First call: discount applied.
            assert calls[0].kwargs.get("amount_override") == _apply_discount(catalogue, 40)
            # Second call: discount already consumed → full catalogue price.
            assert calls[1].kwargs.get("amount_override") == catalogue
            assert calls[1].kwargs.get("extra_metadata") is None

        temp_db.run_until_complete(_run())


# ── cb_check_payment validation against stored amount ──────────────────────


class TestCheckPaymentDiscountedAmount:
    def test_discounted_payment_activates(self, temp_db):
        async def _run():
            user_id = 20
            await db_mod.add_user(user_id, "u", "U")
            catalogue = config.price_for(TIER_PRO, "monthly")
            discounted = _apply_discount(catalogue, 25)
            assert discounted < catalogue
            # Persist a payment row at the *discounted* price (as the
            # discount flow would).
            await db_mod.save_payment(
                user_id, "yk_25off", float(discounted), TIER_PRO, "monthly",
            )

            cb = _make_callback("check_pay:yk_25off", user_id=user_id)
            yk_response = {
                "payment_id": "yk_25off",
                "status": "succeeded",
                "paid": True,
                "amount": str(discounted),
                "metadata": {},
            }
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value=yk_response),
            ):
                await cb_check_payment(cb)
            sub = await db_mod.get_active_subscription(user_id)
            assert sub is not None
            assert sub["tier"] == TIER_PRO

        temp_db.run_until_complete(_run())

    def test_user_paid_only_discounted_but_we_stored_full_price_rejected(
        self, temp_db,
    ):
        """Defensive: if our DB row says we expect the full catalogue price
        (no discount applied at create time) and YooKassa says the user paid
        the discounted amount, we MUST reject — otherwise an attacker can
        ‘negotiate’ their price down post-hoc."""
        async def _run():
            user_id = 21
            await db_mod.add_user(user_id, "u", "U")
            catalogue = config.price_for(TIER_PRO, "monthly")
            await db_mod.save_payment(
                user_id, "yk_underpay", float(catalogue), TIER_PRO, "monthly",
            )

            cb = _make_callback("check_pay:yk_underpay", user_id=user_id)
            yk_response = {
                "payment_id": "yk_underpay",
                "status": "succeeded",
                "paid": True,
                "amount": str(catalogue // 2),  # paid half
                "metadata": {},
            }
            with patch(
                "bot.handlers.payment.check_payment",
                new=MagicMock(return_value=yk_response),
            ):
                await cb_check_payment(cb)
            assert await db_mod.get_active_subscription(user_id) is None

        temp_db.run_until_complete(_run())


# ── get_promo_code expired filter ──────────────────────────────────────────


class TestGetPromoCodeExpired:
    def test_active_code_returned(self, temp_db):
        async def _run():
            await db_mod.create_promo_code(
                "FRESH", PROMO_KIND_BONUS_DAYS, 7,
                expires_at="2099-12-31 00:00:00",
            )
            assert (await db_mod.get_promo_code("FRESH")) is not None

        temp_db.run_until_complete(_run())

    def test_expired_code_hidden(self, temp_db):
        async def _run():
            await db_mod.create_promo_code(
                "STALE", PROMO_KIND_BONUS_DAYS, 7,
                expires_at="2020-01-01 00:00:00",
            )
            assert await db_mod.get_promo_code("STALE") is None

        temp_db.run_until_complete(_run())

    def test_no_expiry_returned(self, temp_db):
        async def _run():
            await db_mod.create_promo_code(
                "FOREVER", PROMO_KIND_BONUS_DAYS, 1,
            )
            assert (await db_mod.get_promo_code("FOREVER")) is not None

        temp_db.run_until_complete(_run())
