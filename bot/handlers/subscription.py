from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import config
from bot.constants import (
    SECTION_ICON_LIGHTNING_ID,
    SECTION_ICON_MONTHLY_ID,
    SECTION_ICON_NO_SUB_ID,
    SECTION_ICON_SUBSCRIPTION_ID,
    SECTION_ICON_TARIFFS_ID,
    SECTION_ICON_TRIAL_ID,
    SECTION_ICON_YEARLY_ID,
    TIER_BADGE_CUSTOM_EMOJI_ID,
    TIER_BADGE_EMOJI,
    TIER_BADGE_LABEL,
    TIER_BASIC,
    TIER_CUSTOM_EMOJI_ID,
    TIER_DELIVERY_COOLDOWN_SECONDS,
    TIER_EMOJI,
    TIER_LABEL,
    TIER_ORDERS_PER_TICK,
    TIER_TAGLINE,
    TIERS,
)
from bot.database import get_active_subscription
from bot.keyboards import subscription_kb
from bot.utils.html import tg_emoji
from bot.utils import safe_edit

router = Router()


def _tier_icon(tier: str) -> str:
    """Render the tier marker as a premium custom emoji with Unicode fallback."""
    return tg_emoji(TIER_CUSTOM_EMOJI_ID.get(tier), TIER_EMOJI[tier])


def _tier_badge(tier: str) -> str:
    """Render the per-tier marketing badge ('Хит'/'All-In') with a premium
    icon prefix. Returns "" when the tier has no badge (Basic)."""
    label = TIER_BADGE_LABEL.get(tier, "")
    if not label:
        return ""
    icon = tg_emoji(
        TIER_BADGE_CUSTOM_EMOJI_ID.get(tier),
        TIER_BADGE_EMOJI.get(tier, ""),
    )
    # Bold label so the badge reads as a tag, not narrative text.
    return f"{icon} <b>{label}</b>"


def _orders_per_hour(tier: str) -> int:
    """Roughly how many orders per hour `tier` can receive at full load."""
    cooldown = TIER_DELIVERY_COOLDOWN_SECONDS[tier]
    if cooldown <= 0:
        return 0
    cycles_per_hour = 3600 // cooldown
    return TIER_ORDERS_PER_TICK[tier] * cycles_per_hour


def _comparison_vs_basic(tier: str) -> str:
    """Marketing copy: how much better `tier` is compared to Basic."""
    if tier == TIER_BASIC:
        return ""
    base_orders = TIER_ORDERS_PER_TICK[TIER_BASIC]
    base_cooldown = TIER_DELIVERY_COOLDOWN_SECONDS[TIER_BASIC]
    my_orders = TIER_ORDERS_PER_TICK[tier]
    my_cooldown = TIER_DELIVERY_COOLDOWN_SECONDS[tier]

    bolt = tg_emoji(SECTION_ICON_LIGHTNING_ID, "\u26a1")
    speed_x = round(base_cooldown / my_cooldown) if my_cooldown else 0
    if my_orders >= 50:
        # "Max" tier — without limit
        return f"{bolt} \u00d7{speed_x} быстрее Basic, без лимита заказов"
    orders_x = round(my_orders / base_orders) if base_orders else 0
    return f"{bolt} \u00d7{orders_x} заказов и \u00d7{speed_x} быстрее Basic"


def _plan_name(plan: str) -> str:
    """Render plan label with its premium icon."""
    if plan == "trial":
        return f"{tg_emoji(SECTION_ICON_TRIAL_ID, chr(0x1F381))} Пробный"
    if plan == "monthly":
        return f"{tg_emoji(SECTION_ICON_MONTHLY_ID, chr(0x1F4C5))} Месячный"
    if plan == "yearly":
        return f"{tg_emoji(SECTION_ICON_YEARLY_ID, chr(0x1F4C6))} Годовой"
    return plan


def _tiers_pricing_block() -> str:
    """Render the table of tiers with pricing, limits and savings.

    The Pro tier is highlighted with a `🌟 Хит` badge and the yearly column
    shows the % discount over 12× monthly. All decorative emoji are wrapped
    in `<tg-emoji>` so Telegram Premium clients render the configured icon.
    """
    header_icon = tg_emoji(SECTION_ICON_TARIFFS_ID, "\U0001f4b0")
    lines = [f"{header_icon} <b>Тарифы:</b>"]
    for tier in TIERS:
        label = TIER_LABEL[tier]
        badge = _tier_badge(tier)
        badge_suffix = f"  {badge}" if badge else ""
        mo = config.price_for(tier, "monthly")
        yr = config.price_for(tier, "yearly")
        savings = config.yearly_savings_pct(tier)
        savings_suffix = f" (\u2212{savings}%)" if savings > 0 else ""
        per_tick = TIER_ORDERS_PER_TICK[tier]
        cooldown_min = TIER_DELIVERY_COOLDOWN_SECONDS[tier] // 60
        per_tick_text = "без лимита" if per_tick >= 50 else f"до {per_tick}"
        comparison = _comparison_vs_basic(tier)
        comparison_line = f"\n   <i>{comparison}</i>" if comparison else ""

        lines.append(
            f"{_tier_icon(tier)} <b>{label}</b>{badge_suffix}\n"
            f"   {mo}\u20bd/мес \u2022 {yr}\u20bd/год{savings_suffix}\n"
            f"   {per_tick_text} заказов / {cooldown_min} мин"
            f" \u2248 {_orders_per_hour(tier)} заказов в час"
            f"{comparison_line}\n"
            f"   <i>{TIER_TAGLINE[tier]}</i>"
        )
    return "\n\n".join(lines)


def format_subscription_info(sub: dict | None) -> str:
    if not sub:
        no_sub_icon = tg_emoji(SECTION_ICON_NO_SUB_ID, "\u274c")
        return (
            f"{no_sub_icon} <b>У тебя нет активной подписки.</b>\n\n"
            "Без подписки бот не будет отправлять заказы.\n\n"
            f"{_tiers_pricing_block()}"
        )

    tier = sub.get("tier") or TIER_BASIC
    tier_label = f"{_tier_icon(tier)} {TIER_LABEL.get(tier, tier)}"
    badge = _tier_badge(tier)
    badge_line = f"  {badge}" if badge else ""

    expires = datetime.fromisoformat(sub["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    days_left = (expires - datetime.now(timezone.utc)).days

    per_tick = TIER_ORDERS_PER_TICK.get(tier, 0)
    cooldown_min = TIER_DELIVERY_COOLDOWN_SECONDS.get(tier, 0) // 60
    per_tick_text = "без лимита" if per_tick >= 50 else f"до {per_tick}"
    comparison = _comparison_vs_basic(tier)
    comparison_line = f"\n{comparison}" if comparison else ""

    star = tg_emoji(SECTION_ICON_SUBSCRIPTION_ID, "\u2b50")
    return (
        f"{star} <b>Твоя подписка</b>\n\n"
        f"<b>Тариф:</b> {tier_label}{badge_line}\n"
        f"<b>Период:</b> {_plan_name(sub['plan'])}\n"
        f"<b>Действует до:</b> {expires.strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"<b>Осталось:</b> {max(days_left, 0)} дней\n\n"
        f"<b>Лимиты тарифа:</b>\n"
        f"• {per_tick_text} заказов за раз\n"
        f"• доставка каждые {cooldown_min} мин"
        f"{comparison_line}"
    )


@router.message(Command("subscription"))
async def cmd_subscription(message: Message):
    sub = await get_active_subscription(message.from_user.id)
    text = format_subscription_info(sub)
    await message.answer(text, reply_markup=subscription_kb(sub is not None))


@router.callback_query(F.data == "subscription")
async def cb_subscription(callback: CallbackQuery):
    sub = await get_active_subscription(callback.from_user.id)
    text = format_subscription_info(sub)
    await safe_edit(callback.message, text, reply_markup=subscription_kb(sub is not None))
    await callback.answer()
