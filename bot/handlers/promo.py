"""Promo code handler.

A user types `/promo CODE` and we either credit them with bonus days (kind =
`bonus_days`) or stash a one-time discount they can use on the next purchase
(kind = `discount_pct`).

Discount-percent codes are intentionally lightweight in this sprint: they are
*recorded* via `redeem_promo_code` and the redemption row carries the
`payment_id = NULL` until the payment flow consumes it. To keep the surface
area small for Sprint 2A, percent discounts are surfaced to the user as a
"come back at checkout — your discount is reserved" message and consumption
during checkout is left to a follow-up sprint. Bonus-days codes, by contrast,
are applied immediately into the bonus_days_ledger and are spent at the next
paid activation just like a referral bonus.
"""

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.constants import (
    PROMO_CODE_MAX_LENGTH,
    PROMO_CODE_MIN_LENGTH,
    PROMO_KIND_BONUS_DAYS,
    PROMO_KIND_DISCOUNT_PCT,
)
from bot.database import (
    add_bonus_days,
    get_promo_code,
    has_user_redeemed_promo,
    log_event,
    redeem_promo_code,
)
from bot.keyboards import back_kb
from bot.utils import safe_edit

router = Router()
logger = logging.getLogger(__name__)


def _normalize_code(raw: str | None) -> str | None:
    """Trim, upper-case, and length-validate a user-supplied code. Returns
    None for inputs that obviously can't match anything in the table."""
    if not raw:
        return None
    code = raw.strip().upper()
    if len(code) < PROMO_CODE_MIN_LENGTH or len(code) > PROMO_CODE_MAX_LENGTH:
        return None
    # Don't try to be clever about charset — the DB will simply not find
    # codes with weird characters and the user gets the generic "not found".
    return code


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    """Activate a promo code.

    Usage: `/promo CODE`. We never reveal *why* a redemption failed
    (already used / expired / exhausted / unknown) — to a casual user they
    all look the same and revealing details lets attackers map the table.
    """
    if not message.from_user:
        return
    user_id = message.from_user.id

    code = _normalize_code(command.args)
    if not code:
        await message.answer(
            "\U0001f3ab <b>Введи промокод</b>\n\n"
            "Использование: <code>/promo CODE</code>\n"
            "Например: <code>/promo SUMMER25</code>",
            reply_markup=back_kb(),
        )
        return

    promo = await get_promo_code(code)

    # Generic failure surface — same message regardless of root cause.
    not_valid_msg = (
        "\u26a0\ufe0f <b>Промокод не подошёл.</b>\n\n"
        "Возможно, он уже использован, истёк или введён с ошибкой."
    )

    if not promo:
        await log_event(
            "promo_failed",
            user_id=user_id,
            properties={"code": code, "reason": "not_found"},
        )
        await message.answer(not_valid_msg, reply_markup=back_kb())
        return

    if await has_user_redeemed_promo(promo["id"], user_id):
        await log_event(
            "promo_failed",
            user_id=user_id,
            properties={"code": code, "reason": "already_redeemed"},
        )
        await message.answer(not_valid_msg, reply_markup=back_kb())
        return

    redeemed = await redeem_promo_code(promo["id"], user_id, payment_id=None)
    if not redeemed:
        await log_event(
            "promo_failed",
            user_id=user_id,
            properties={"code": code, "reason": "race_or_exhausted"},
        )
        await message.answer(not_valid_msg, reply_markup=back_kb())
        return

    kind = promo["kind"]
    value = int(promo["value"])

    if kind == PROMO_KIND_BONUS_DAYS:
        await add_bonus_days(
            user_id, value, source="promo", source_ref=code,
        )
        await log_event(
            "promo_redeemed",
            user_id=user_id,
            properties={"code": code, "kind": kind, "value": value},
        )
        await message.answer(
            f"\u2705 <b>Промокод активирован!</b>\n\n"
            f"\U0001f381 +{value} дней к подписке.\n"
            f"Они применятся при следующей оплате.",
            reply_markup=back_kb(),
        )
        return

    if kind == PROMO_KIND_DISCOUNT_PCT:
        await log_event(
            "promo_redeemed",
            user_id=user_id,
            properties={"code": code, "kind": kind, "value": value},
        )
        # The discount is reserved as an unconsumed redemption row. The
        # next time the user starts a YooKassa checkout via /subscription,
        # `_start_yookassa` looks it up and applies the discount. We tell the
        # user it applies only to ЮKassa — Stars invoices keep the catalogue
        # price (consistent with the create_payment-only consumption hook).
        await message.answer(
            f"\u2705 <b>Промокод активирован!</b>\n\n"
            f"\U0001f4b8 Скидка {value}% будет применена при следующей "
            f"оплате через <b>ЮKassa</b>.\n"
            f"<i>(Telegram Stars скидку не поддерживают.)</i>",
            reply_markup=back_kb(),
        )
        return

    # Unknown kind — should never happen if the admin tool validated input,
    # but we don't want a runtime error to leave the user without a reply.
    logger.error("Unknown promo kind: %r", kind)
    await message.answer(not_valid_msg, reply_markup=back_kb())
