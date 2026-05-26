from aiogram import Router, F
from aiogram.types import CallbackQuery

from bot.constants import (
    LATEST_ORDERS_DISPLAY_LIMIT,
    ORDER_DESCRIPTION_PREVIEW,
    ORDER_ICON_CATEGORY_ID,
    ORDER_ICON_LINK_ID,
    ORDER_ICON_PLATFORM_ID,
    ORDER_ICON_TITLE_ID,
    ORDER_MESSAGE_MAX_LENGTH,
)
from bot.database import (
    get_active_subscription,
    get_unsent_orders_for_user,
    mark_order_sent,
)
from bot.keyboards import back_kb
from bot.utils.html import html_escape, safe_url, tg_emoji, to_bold_digits, truncate
from bot.utils import safe_edit

router = Router()

PLATFORM_EMOJI = {
    "Kwork": "\U0001f7e2",
    "FL.ru": "\U0001f535",
    "Freelance.ru": "\U0001f7e0",
    "Weblancer": "\U0001f534",
    "YouDo": "\U0001f7e1",
}


def format_order(order: dict) -> str:
    """Render an order as a Telegram-HTML-safe message.

    All free-form fields are HTML-escaped before substitution; the final
    message is also clamped to Telegram's 4096-char limit.
    """
    platform = order["platform"]
    platform_fallback = PLATFORM_EMOJI.get(platform, "\u25ab\ufe0f")
    raw_price = order.get("price")
    category = html_escape(order["category"]) if order.get("category") else "не указана"

    raw_description = order.get("description") or ""
    description = html_escape(truncate(raw_description, ORDER_DESCRIPTION_PREVIEW))
    title = html_escape(order["title"])
    url = safe_url(order.get("url"))

    platform_icon = tg_emoji(ORDER_ICON_PLATFORM_ID, platform_fallback)
    title_icon = tg_emoji(ORDER_ICON_TITLE_ID, "\U0001f4cc")
    category_icon = tg_emoji(ORDER_ICON_CATEGORY_ID, "\U0001f4c1")
    link_icon = tg_emoji(ORDER_ICON_LINK_ID, "\U0001f517")

    # Each section is separated by a blank line for visual breathing room.
    sections = [
        f"{platform_icon} <b>{html_escape(platform)}</b>",
        f"{title_icon} <b>{title}</b>",
    ]
    if raw_price:
        sections.append(f"<b>{html_escape(to_bold_digits(raw_price))}</b>")
    sections.append(f"{category_icon} Категория: {category}")
    if description:
        sections.append(f"<i>{description}</i>")
    sections.append(f"{link_icon} <a href=\"{url}\">Открыть заказ</a>")

    message = "\n\n".join(sections)
    if len(message) > ORDER_MESSAGE_MAX_LENGTH:
        message = message[: ORDER_MESSAGE_MAX_LENGTH - 1] + "…"
    return message


@router.callback_query(F.data == "latest_orders")
async def cb_latest_orders(callback: CallbackQuery):
    user_id = callback.from_user.id

    sub = await get_active_subscription(user_id)
    if not sub:
        await safe_edit(callback.message,
            "\u274c <b>Нет активной подписки.</b>\n\n"
            "Оформи подписку, чтобы получать заказы.",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    orders = await get_unsent_orders_for_user(user_id)
    if not orders:
        await safe_edit(callback.message,
            "\U0001f4ed <b>Новых заказов пока нет.</b>\n\n"
            "Бот проверяет площадки периодически.\n"
            "Убедись, что выбраны категории и площадки.",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    await safe_edit(callback.message,
        f"\U0001f4cb <b>Найдено {len(orders)} новых заказов:</b>",
        reply_markup=back_kb(),
    )

    for order in orders[:LATEST_ORDERS_DISPLAY_LIMIT]:
        await callback.message.answer(
            format_order(order),
            disable_web_page_preview=True,
        )
        await mark_order_sent(user_id, order["id"])

    await callback.answer()
