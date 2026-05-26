import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from bot.config import config
from bot.constants import TIER_LABEL, TIERS
from bot.database import (
    activate_subscription,
    get_active_subscription,
    get_payment,
    get_pending_discount_for_user,
    log_event,
    mark_discount_redemption_consumed,
    save_payment,
    update_payment_status,
)
from bot.keyboards import back_kb, main_menu_kb
from bot.payments.amounts import amount_matches
from bot.payments.yookassa import check_payment, create_payment, expected_amount
from bot.utils import safe_edit

router = Router()
logger = logging.getLogger(__name__)


# Currencies we accept. Kept as constants so the parser can validate against
# them and we don't sprinkle string literals through the file.
CURRENCY_RUB = "RUB"
CURRENCY_XTR = "XTR"

# `payments.payment_id` for Stars is synthetic (Telegram does not give us one
# until checkout). We stamp invoices we generate with this prefix so the
# pre_checkout handler can reliably round-trip them through `payload`.
STARS_PAYMENT_ID_PREFIX = "stars-"


def _plan_label(plan: str) -> str:
    return "Годовая" if plan == "yearly" else "Месячная"


def _parse_pay_callback(data: str) -> tuple[str, str] | None:
    """Parse `pay:<tier>:<plan>`. Returns None if malformed/unknown.

    We deliberately only accept the canonical 3-segment form and reject the
    legacy `pay:<plan>` form so a malicious client can't smuggle a Max-tier
    activation by replaying an old callback.
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "pay":
        return None
    _, tier, plan = parts
    if tier not in TIERS or plan not in ("monthly", "yearly"):
        return None
    return tier, plan


def _parse_method_callback(data: str) -> tuple[str, str, str] | None:
    """Parse `paym:<method>:<tier>:<plan>`. Returns (method, tier, plan)."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "paym":
        return None
    _, method, tier, plan = parts
    if method not in ("rub", "xtr"):
        return None
    if tier not in TIERS or plan not in ("monthly", "yearly"):
        return None
    return method, tier, plan


def _parse_stars_payload(payload: str) -> tuple[int, str, str] | None:
    """Reverse of how we build a Stars `invoice_payload`. Format:
    `sub:<user_id>:<tier>:<plan>:<nonce>`. The nonce is opaque to us;
    it just makes each invoice payload distinct so users can issue multiple
    Stars invoices in a row."""
    parts = (payload or "").split(":")
    if len(parts) != 5 or parts[0] != "sub":
        return None
    try:
        uid = int(parts[1])
    except ValueError:
        return None
    tier, plan = parts[2], parts[3]
    if tier not in TIERS or plan not in ("monthly", "yearly"):
        return None
    return uid, tier, plan


def _payment_method_kb(tier: str, plan: str) -> InlineKeyboardMarkup:
    """Two-button picker shown when both YooKassa and Stars are enabled."""
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text=(
                f"\U0001f4b3 ЮKassa \u2014 "
                f"{config.price_for(tier, plan)}\u20bd"
            ),
            callback_data=f"paym:rub:{tier}:{plan}",
        )
    ]]
    if config.stars_enabled:
        rows.append([InlineKeyboardButton(
            text=(
                f"\u2b50 Stars \u2014 "
                f"{config.stars_price_for(tier, plan)} XTR"
            ),
            callback_data=f"paym:xtr:{tier}:{plan}",
        )])
    rows.append([InlineKeyboardButton(text="\u2b05 Отмена", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(callback: CallbackQuery):
    """Step 1: tier + plan chosen — show payment-method picker.

    If Stars is disabled, skip the picker and fall straight through to
    YooKassa to keep the legacy single-method flow intact.
    """
    user_id = callback.from_user.id
    parsed = _parse_pay_callback(callback.data or "")
    if not parsed:
        await callback.answer("Некорректный тариф", show_alert=True)
        return
    tier, plan = parsed

    sub = await get_active_subscription(user_id)
    if sub and sub["plan"] != "trial":
        await safe_edit(callback.message,
            "\u2705 У тебя уже есть активная подписка!",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    await log_event(
        "payment_started",
        user_id=user_id,
        properties={"tier": tier, "plan": plan},
    )

    if not config.stars_enabled:
        await _start_yookassa(callback, tier, plan)
        return

    await safe_edit(callback.message,
        f"\U0001f4b3 <b>Выбери способ оплаты</b>\n\n"
        f"<b>Тариф:</b> {TIER_LABEL[tier]} ({_plan_label(plan)})\n\n"
        f"\u2022 <b>ЮKassa</b> \u2014 банковская карта (РФ)\n"
        f"\u2022 <b>Telegram Stars</b> \u2014 встроенная валюта Telegram, "
        f"работает в любой стране",
        reply_markup=_payment_method_kb(tier, plan),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("paym:"))
async def cb_payment_method(callback: CallbackQuery):
    """Step 2: method chosen — fork to YooKassa or Stars."""
    parsed = _parse_method_callback(callback.data or "")
    if not parsed:
        await callback.answer("Некорректный способ", show_alert=True)
        return
    method, tier, plan = parsed
    if method == "xtr":
        await _start_stars(callback, tier, plan)
    else:
        await _start_yookassa(callback, tier, plan)


def _apply_discount(base_amount: int, pct: int) -> int:
    """Apply a percent discount to `base_amount`, rounding down to a whole ₽.

    Pinned to a minimum of 1 ₽ so YooKassa never sees a 0-amount invoice
    (which it would reject).
    """
    pct = max(0, min(100, int(pct)))
    return max(1, base_amount * (100 - pct) // 100)


async def _start_yookassa(callback: CallbackQuery, tier: str, plan: str) -> None:
    """Existing YooKassa flow, factored out so both `cb_pay` (when Stars is
    disabled) and `cb_payment_method` can call it."""
    user_id = callback.from_user.id

    # Apply a pending discount_pct redemption if the user activated one via
    # /promo. We consume the redemption immediately on invoice creation: if
    # the user abandons the checkout the discount is forfeited (matches how
    # most real coupons work). The discount only applies to YooKassa — Stars
    # invoices keep the catalogue price.
    discount = await get_pending_discount_for_user(user_id)
    base_amount = expected_amount(tier, plan)
    if discount:
        amount = _apply_discount(base_amount, int(discount["pct"]))
        extra_md = {
            "promo_code": str(discount["code"]),
            "discount_pct": str(int(discount["pct"])),
            "promo_redemption_id": str(int(discount["redemption_id"])),
        }
    else:
        amount = base_amount
        extra_md = None

    result = create_payment(
        user_id, tier, plan,
        amount_override=amount,
        extra_metadata=extra_md,
    )
    if not result:
        await safe_edit(callback.message,
            "\u274c <b>Ошибка создания платежа.</b>\n\n"
            "Попробуй позже или обратись в поддержку.",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    await save_payment(
        user_id,
        result["payment_id"],
        float(result["amount"]),
        tier,
        plan,
        provider="yookassa",
        currency=CURRENCY_RUB,
    )
    if discount:
        # Stamp the redemption so a second checkout doesn't re-apply the
        # same code. If the user actually pays this invoice, great; if they
        # abandon, the discount is gone (documented in the UX below).
        await mark_discount_redemption_consumed(
            int(discount["redemption_id"]), result["payment_id"],
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4b3 Оплатить", url=result["confirmation_url"])],
        [InlineKeyboardButton(
            text="\u2705 Я оплатил",
            callback_data=f"check_pay:{result['payment_id']}",
        )],
        [InlineKeyboardButton(text="\u2b05 Отмена", callback_data="menu")],
    ])

    if discount:
        discount_line = (
            f"\U0001f381 <b>Скидка по промокоду {discount['code']}:</b> "
            f"\u2212{int(discount['pct'])}%  "
            f"(<s>{base_amount}\u20bd</s> \u2192 <b>{result['amount']}\u20bd</b>)\n\n"
        )
    else:
        discount_line = ""

    await safe_edit(callback.message,
        f"\U0001f4b3 <b>Оплата подписки</b>\n\n"
        f"<b>Тариф:</b> {TIER_LABEL[tier]} ({_plan_label(plan)})\n"
        f"<b>Сумма:</b> {result['amount']}\u20bd\n\n"
        f"{discount_line}"
        f"Нажми кнопку «Оплатить» — откроется страница ЮKassa.\n"
        f"После оплаты нажми «Я оплатил».",
        reply_markup=kb,
    )
    await callback.answer()


async def _start_stars(callback: CallbackQuery, tier: str, plan: str) -> None:
    """Send a Telegram Stars invoice. Telegram handles UI / payment;
    we only see `pre_checkout_query` and (on success) `successful_payment`."""
    user_id = callback.from_user.id
    amount = config.stars_price_for(tier, plan)

    # Random nonce so users can re-issue invoices in a row without payload
    # collisions (Telegram dedups invoices with identical payloads).
    import secrets as _secrets
    nonce = _secrets.token_hex(4)
    payload = f"sub:{user_id}:{tier}:{plan}:{nonce}"

    bot = callback.bot
    try:
        # Acknowledge the callback before sending the invoice to dismiss the
        # loading spinner — the invoice itself is a separate message.
        await callback.answer()
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=f"Подписка {TIER_LABEL[tier]} ({_plan_label(plan).lower()})",
            description=(
                "Подписка на парсер заказов с фриланс-площадок. "
                "Оплата через Telegram Stars."
            ),
            payload=payload,
            provider_token="",  # empty for XTR (Stars)
            currency=CURRENCY_XTR,
            prices=[LabeledPrice(
                label=f"{TIER_LABEL[tier]} ({_plan_label(plan).lower()})",
                amount=amount,
            )],
            start_parameter=f"sub_{tier}_{plan}",
        )
    except TelegramBadRequest as exc:
        logger.warning("Failed to send Stars invoice: %s", exc)
        await callback.message.answer(
            "\u26a0\ufe0f Не удалось создать счёт в Stars. Попробуй ЮKassa.",
            reply_markup=back_kb(),
        )


@router.pre_checkout_query()
async def cb_pre_checkout(query: PreCheckoutQuery):
    """Telegram asks us to confirm an invoice immediately before charging the
    user. We must answer within ~10s. We validate the payload and price
    against our table — if anything is off we reject and the user is not
    charged.
    """
    payload_parts = _parse_stars_payload(query.invoice_payload)
    if not payload_parts:
        await query.answer(
            ok=False,
            error_message="Платёж устарел. Открой подписку заново.",
        )
        return
    payload_uid, tier, plan = payload_parts

    if payload_uid != query.from_user.id:
        await query.answer(ok=False, error_message="Несоответствие пользователя.")
        return

    if query.currency != CURRENCY_XTR:
        await query.answer(ok=False, error_message="Несоответствие валюты.")
        return

    expected = config.stars_price_for(tier, plan)
    if int(query.total_amount) != int(expected):
        await query.answer(ok=False, error_message="Несоответствие суммы.")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def cb_successful_payment(message: Message):
    """Telegram delivers a `successful_payment` message after a Stars charge
    completes. This is the source of truth — we activate the subscription
    only here, never on `pre_checkout`.
    """
    sp = message.successful_payment
    if not sp:
        return

    payload_parts = _parse_stars_payload(sp.invoice_payload)
    if not payload_parts:
        logger.error(
            "Successful payment with unparseable payload: %r",
            sp.invoice_payload,
        )
        return
    payload_uid, tier, plan = payload_parts
    if payload_uid != message.from_user.id:
        logger.error(
            "Successful payment user mismatch: payload=%s message=%s",
            payload_uid, message.from_user.id,
        )
        return

    # The Telegram-issued payment id is the most stable identifier we get.
    # We prefix it so it can never collide with a YooKassa id.
    payment_id = f"{STARS_PAYMENT_ID_PREFIX}{sp.telegram_payment_charge_id}"

    # Save first, then activate — if activation crashes the row stays in
    # `pending` so we can replay it manually.
    try:
        await save_payment(
            user_id=message.from_user.id,
            payment_id=payment_id,
            amount=float(sp.total_amount),
            tier=tier,
            plan=plan,
            provider="stars",
            currency=CURRENCY_XTR,
        )
    except Exception:
        # Most likely a duplicate id (unique constraint) — already paid.
        existing = await get_payment(payment_id)
        if existing and existing.get("status") == "succeeded":
            return

    await update_payment_status(payment_id, "succeeded")
    await activate_subscription(
        message.from_user.id, tier, plan, payment_id
    )
    await log_event(
        "payment_succeeded",
        user_id=message.from_user.id,
        properties={
            "tier": tier,
            "plan": plan,
            "provider": "stars",
            "amount": int(sp.total_amount),
            "currency": CURRENCY_XTR,
        },
    )

    await message.answer(
        f"\u2705 <b>Оплата прошла успешно!</b>\n\n"
        f"Подписка <b>{TIER_LABEL[tier]}</b> активирована. "
        f"Теперь ты будешь получать заказы.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("check_pay:"))
async def cb_check_payment(callback: CallbackQuery):
    """Verify a payment.

    Important: we never trust `tier`, `plan` or `amount` from the callback
    data — a malicious user could otherwise activate a Max plan after only
    paying for Basic, or activate someone else's payment. Instead we look up
    the payment in our DB by id and check that:
      * the payment belongs to the calling user;
      * the YooKassa-reported amount matches the expected price for the
        stored (tier, plan);
      * the payment is in `succeeded` state.
    """
    user_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    payment_id = parts[1]

    payment_row = await get_payment(payment_id)
    if not payment_row or payment_row["user_id"] != user_id:
        # Either the payment doesn't exist, or it belongs to someone else.
        # Don't leak that distinction to the caller.
        await callback.answer("Платёж не найден", show_alert=True)
        return

    tier = payment_row.get("tier")
    plan = payment_row["plan"]
    if tier not in TIERS or plan not in ("monthly", "yearly"):
        await callback.answer("Некорректный тариф", show_alert=True)
        return

    # Idempotency: if this payment was already marked succeeded (the user is
    # spam-clicking "Я оплатил"), do NOT re-run activate_subscription —
    # it would extend the expiry by another 30 days for free.
    if payment_row.get("status") == "succeeded":
        await safe_edit(callback.message,
            f"\u2705 Подписка <b>{TIER_LABEL[tier]}</b> уже активирована.",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    await callback.answer("\u23f3 Проверяю оплату...")

    # The stored `amount` is the price we *actually quoted* the user — i.e.
    # the catalogue price MINUS any promo-code discount applied during
    # checkout. We must validate against that, not against the catalogue price,
    # otherwise legitimate discounted payments would be rejected.
    expected_paid = payment_row["amount"]

    for attempt in range(3):
        result = check_payment(payment_id)
        if (
            result
            and result["status"] == "succeeded"
            and result["paid"]
            and amount_matches(result["amount"], expected_paid)
        ):
            await update_payment_status(payment_id, "succeeded")
            await activate_subscription(user_id, tier, plan, payment_id)
            await log_event(
                "payment_succeeded",
                user_id=user_id,
                properties={
                    "tier": tier,
                    "plan": plan,
                    "provider": "yookassa",
                    "amount": float(result["amount"]),
                    "currency": CURRENCY_RUB,
                },
            )

            await safe_edit(callback.message,
                f"\u2705 <b>Оплата прошла успешно!</b>\n\n"
                f"Подписка <b>{TIER_LABEL[tier]}</b> активирована. "
                f"Теперь ты будешь получать заказы.\n"
                f"Убедись, что настроены категории и площадки.",
                reply_markup=main_menu_kb(),
            )
            return

        if (
            result
            and result["status"] == "succeeded"
            and not amount_matches(result["amount"], expected_paid)
        ):
            logger.warning(
                "Amount mismatch for payment %s: got %s, expected %s "
                "(tier=%s, plan=%s)",
                payment_id, result["amount"], expected_paid, tier, plan,
            )
            await safe_edit(callback.message,
                "\u26a0\ufe0f Сумма платежа не совпадает с тарифом. "
                "Свяжись с поддержкой.",
                reply_markup=back_kb(),
            )
            return

        if attempt < 2:
            await asyncio.sleep(2)

    await safe_edit(callback.message,
        "\u23f3 <b>Оплата ещё не подтверждена.</b>\n\n"
        "Если ты только что оплатил — подожди 1-2 минуты и нажми "
        "«Я оплатил» снова.",
        reply_markup=back_kb(),
    )


def _amount_matches(value: object, tier: str, plan: str) -> bool:
    return amount_matches(value, expected_amount(tier, plan))
