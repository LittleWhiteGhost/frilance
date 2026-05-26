"""Thin wrapper around the YooKassa SDK with input validation."""

from __future__ import annotations

import logging
import uuid

from yookassa import Configuration, Payment

from bot.config import config
from bot.constants import TIER_LABEL, TIERS

logger = logging.getLogger(__name__)


def _ensure_configured() -> bool:
    """Lazily push credentials into the SDK so config validation can run first."""
    if not config.yookassa_shop_id or not config.yookassa_secret_key:
        logger.error("YooKassa credentials are not configured")
        return False
    Configuration.account_id = config.yookassa_shop_id
    Configuration.secret_key = config.yookassa_secret_key
    return True


def expected_amount(tier: str, plan: str) -> int:
    """Lookup the configured price for `tier` × `plan`."""
    return config.price_for(tier, plan)


def _return_url() -> str:
    if config.yookassa_return_url:
        return config.yookassa_return_url
    if config.bot_username:
        return f"https://t.me/{config.bot_username.lstrip('@')}"
    # Fallback: a static success page acceptable to YooKassa.
    return "https://yookassa.ru/"


def create_payment(
    user_id: int,
    tier: str,
    plan: str,
    *,
    amount_override: int | None = None,
    extra_metadata: dict | None = None,
) -> dict | None:
    """Create a YooKassa payment for `user_id` × `tier` × `plan`.

    `amount_override` lets the caller charge less than the catalogue price —
    used by the promo-discount flow. The override must be > 0; otherwise the
    catalogue price is used. We never quietly fall back to the catalogue price
    when an override is provided but invalid: that would over-charge a user
    who legitimately had a discount.
    """
    if not _ensure_configured():
        return None
    if tier not in TIERS:
        logger.error("Invalid tier: %s", tier)
        return None
    if plan not in ("monthly", "yearly"):
        logger.error("Invalid plan: %s", plan)
        return None

    catalogue_amount = expected_amount(tier, plan)
    if amount_override is not None:
        if amount_override <= 0:
            logger.error(
                "Invalid amount_override %s for %s %s",
                amount_override, tier, plan,
            )
            return None
        amount = int(amount_override)
    else:
        amount = catalogue_amount
    period = "Годовая" if plan == "yearly" else "Месячная"
    description = (
        f"{period} подписка на FreelanceParser Bot — тариф {TIER_LABEL[tier]}"
    )

    metadata: dict[str, str] = {
        "user_id": str(user_id),
        "tier": tier,
        "plan": plan,
    }
    if extra_metadata:
        # Stringify everything — YooKassa stores metadata as strings.
        for k, v in extra_metadata.items():
            metadata[str(k)] = str(v)

    try:
        payment = Payment.create({
            "amount": {
                "value": str(amount),
                "currency": "RUB",
            },
            "confirmation": {
                "type": "redirect",
                "return_url": _return_url(),
            },
            "capture": True,
            "description": description,
            "metadata": metadata,
        }, uuid.uuid4().hex)

        return {
            "payment_id": payment.id,
            "confirmation_url": payment.confirmation.confirmation_url,
            "amount": amount,
        }
    except Exception:
        logger.exception("YooKassa create_payment failed")
        return None


def check_payment(payment_id: str) -> dict | None:
    if not _ensure_configured():
        return None
    try:
        payment = Payment.find_one(payment_id)
        return {
            "payment_id": payment.id,
            "status": payment.status,
            "paid": payment.paid,
            "amount": payment.amount.value,
            "metadata": payment.metadata,
        }
    except Exception:
        logger.exception("YooKassa check_payment failed for %s", payment_id)
        return None
