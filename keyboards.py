from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import config
from bot.constants import (
    PLATFORM_REGISTRY,
    TIER_BADGE_EMOJI,
    TIER_BADGE_LABEL,
    TIER_EMOJI,
    TIER_LABEL,
    TIERS,
)

CATEGORIES = [
    "Программирование",
    "Веб-разработка",
    "Мобильная разработка",
    "Дизайн",
    "Тексты и переводы",
    "Маркетинг и реклама",
    "SEO и трафик",
    "Аудио и видео",
    "Бизнес и консалтинг",
    "Разное",
]

# Ordered list of (code, display_name) for all supported platforms.
# Emoji prefix makes it easier to scan on mobile.
PLATFORMS: list[tuple[str, str]] = [
    (code, f"{meta['emoji']} {meta['name']}")
    for code, meta in PLATFORM_REGISTRY.items()
]

PLATFORM_NAME_BY_CODE: dict[str, str] = {
    code: meta["name"] for code, meta in PLATFORM_REGISTRY.items()
}


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Мои категории", callback_data="my_categories")],
        [InlineKeyboardButton(text="🌐 Мои площадки", callback_data="my_platforms")],
        [InlineKeyboardButton(text="📋 Последние заказы", callback_data="latest_orders")],
        [InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def categories_kb(selected: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for cat in CATEGORIES:
        check = "✅ " if cat in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{check}{cat}",
            callback_data=f"cat_toggle:{cat}",
        )])
    buttons.append([InlineKeyboardButton(text="✔️ Готово", callback_data="cat_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def platforms_kb(selected: list[str]) -> InlineKeyboardMarkup:
    """Platform picker keyboard.

    Shows all 8 platforms. Selected ones get a ✅ prefix.
    Platforms are laid out 1-per-row so the emoji + name fits on mobile.
    """
    buttons = []
    for code, display_name in PLATFORMS:
        check = "✅ " if code in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{check}{display_name}",
            callback_data=f"plat_toggle:{code}",
        )])
    buttons.append([InlineKeyboardButton(text="✔️ Готово", callback_data="plat_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subscription_kb(has_active: bool) -> InlineKeyboardMarkup:
    if has_active:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Назад", callback_data="menu")],
        ])

    rows: list[list[InlineKeyboardButton]] = []
    for tier in TIERS:
        b_emoji = TIER_BADGE_EMOJI.get(tier, "")
        b_label = TIER_BADGE_LABEL.get(tier, "")
        badge_suffix = f" • {b_emoji} {b_label}".rstrip() if b_label else ""
        savings = config.yearly_savings_pct(tier)
        savings_suffix = f" (−{savings}%)" if savings > 0 else ""

        rows.append([InlineKeyboardButton(
            text=(
                f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]} — мес "
                f"{config.price_for(tier, 'monthly')}₽{badge_suffix}"
            ),
            callback_data=f"pay:{tier}:monthly",
        )])
        rows.append([InlineKeyboardButton(
            text=(
                f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]} — год "
                f"{config.price_for(tier, 'yearly')}₽{savings_suffix}"
            ),
            callback_data=f"pay:{tier}:yearly",
        )])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅ Назад в меню", callback_data="menu")],
    ])
