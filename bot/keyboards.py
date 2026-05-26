from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import config
from bot.constants import (
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

PLATFORMS = [
    ("Kwork", "kwork"),
    ("FL.ru", "fl_ru"),
    ("Freelance.ru", "freelance_ru"),
    ("Weblancer", "weblancer"),
    ("YouDo", "youdo"),
]

PLATFORM_NAME_BY_CODE = {code: name for name, code in PLATFORMS}


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f50d Мои категории", callback_data="my_categories")],
        [InlineKeyboardButton(text="\U0001f310 Мои площадки", callback_data="my_platforms")],
        [InlineKeyboardButton(text="\U0001f4cb Последние заказы", callback_data="latest_orders")],
        [InlineKeyboardButton(text="\u2b50 Подписка", callback_data="subscription")],
        [InlineKeyboardButton(text="\u2753 Помощь", callback_data="help")],
    ])


def categories_kb(selected: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for cat in CATEGORIES:
        check = "\u2705 " if cat in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{check}{cat}",
            callback_data=f"cat_toggle:{cat}",
        )])
    buttons.append([InlineKeyboardButton(text="\u2714\ufe0f Готово", callback_data="cat_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def platforms_kb(selected: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for name, code in PLATFORMS:
        check = "\u2705 " if code in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{check}{name}",
            callback_data=f"plat_toggle:{code}",
        )])
    buttons.append([InlineKeyboardButton(text="\u2714\ufe0f Готово", callback_data="plat_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subscription_kb(has_active: bool) -> InlineKeyboardMarkup:
    if has_active:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2b05 Назад", callback_data="menu")],
        ])

    # Two buttons per tier (monthly + yearly). 6 buttons total + back.
    rows: list[list[InlineKeyboardButton]] = []
    for tier in TIERS:
        # Append a marketing badge on the monthly button so Pro/Max stand out
        # in the list. Yearly buttons get a savings % instead.
        # Inline-keyboard buttons can't render premium <tg-emoji>, so we use
        # plain Unicode here.
        b_emoji = TIER_BADGE_EMOJI.get(tier, "")
        b_label = TIER_BADGE_LABEL.get(tier, "")
        badge_suffix = f" \u2022 {b_emoji} {b_label}".rstrip() if b_label else ""
        savings = config.yearly_savings_pct(tier)
        savings_suffix = f" (\u2212{savings}%)" if savings > 0 else ""

        rows.append([InlineKeyboardButton(
            text=(
                f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]} \u2014 мес "
                f"{config.price_for(tier, 'monthly')}\u20bd{badge_suffix}"
            ),
            callback_data=f"pay:{tier}:monthly",
        )])
        rows.append([InlineKeyboardButton(
            text=(
                f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]} \u2014 год "
                f"{config.price_for(tier, 'yearly')}\u20bd{savings_suffix}"
            ),
            callback_data=f"pay:{tier}:yearly",
        )])
    rows.append([InlineKeyboardButton(text="\u2b05 Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2b05 Назад в меню", callback_data="menu")],
    ])
