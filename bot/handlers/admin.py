"""Admin-only commands: stats, broadcast, user listing."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import config
from bot.constants import (
    ADMIN_USERS_LIST_LIMIT,
    GLOBAL_BROADCAST_INTERVAL_SECONDS,
    PROMO_KIND_BONUS_DAYS,
    PROMO_KIND_DISCOUNT_PCT,
    PROMO_KINDS,
)
from bot.database import (
    create_promo_code,
    get_all_active_users,
    get_orders_count,
    get_subscribed_users,
    get_users_count,
    mark_user_inactive,
)
from bot.utils.html import html_escape
from bot.utils import safe_edit

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


def _broadcast_text_from_message(message: Message) -> str:
    """Extract broadcast body, handling /broadcast@bot_username style too."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("\u274c Доступ запрещён.")
        return

    users_count = await get_users_count()
    orders_count = await get_orders_count()
    subs = await get_subscribed_users()

    text = (
        "\U0001f6e0 <b>Админ-панель</b>\n\n"
        f"\U0001f465 Всего пользователей: {users_count}\n"
        f"\u2b50 Активных подписчиков: {len(subs)}\n"
        f"\U0001f4cb Заказов в базе: {orders_count}\n"
        f"\u23f0 Интервал парсинга: {config.parse_interval} мин\n"
    )

    await message.answer(text)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("\u274c Доступ запрещён.")
        return

    broadcast_text = _broadcast_text_from_message(message)
    if not broadcast_text:
        await message.answer("Использование: /broadcast <текст сообщения>")
        return

    safe_body = html_escape(broadcast_text)
    payload = f"\U0001f4e2 <b>Объявление:</b>\n\n{safe_body}"

    users = await get_all_active_users()
    sent = 0
    failed = 0
    blocked = 0
    last_send = 0.0

    for user in users:
        user_id = user["user_id"]

        elapsed = time.monotonic() - last_send
        if elapsed < GLOBAL_BROADCAST_INTERVAL_SECONDS:
            await asyncio.sleep(GLOBAL_BROADCAST_INTERVAL_SECONDS - elapsed)

        delivered = False
        for attempt in range(3):
            try:
                await message.bot.send_message(chat_id=user_id, text=payload)
                delivered = True
                break
            except TelegramRetryAfter as exc:
                wait = float(getattr(exc, "retry_after", 1)) + 0.5
                logger.warning("Broadcast FloodWait for %s: %.1fs", user_id, wait)
                await asyncio.sleep(wait)
            except TelegramForbiddenError:
                logger.info("User %s blocked the bot — marking inactive", user_id)
                await mark_user_inactive(user_id)
                blocked += 1
                break
            except Exception:
                logger.exception("Broadcast failed for %s (attempt %s)", user_id, attempt + 1)
                await asyncio.sleep(1)

        last_send = time.monotonic()
        if delivered:
            sent += 1
        elif blocked == 0 or user_id != users[-1]["user_id"]:
            # Counts: anything not delivered and not already counted as blocked.
            failed += 1

    summary = (
        "\u2705 Рассылка завершена.\n"
        f"Отправлено: {sent}\n"
        f"Заблокировали бот: {blocked}\n"
        f"Ошибок: {failed - blocked if failed >= blocked else failed}"
    )
    await message.answer(summary)


@router.message(Command("promo_create"))
async def cmd_promo_create(message: Message) -> None:
    """Mint a promo code from chat. Usage:

        /promo_create CODE bonus_days 7 [max_uses=10]
        /promo_create CODE discount_pct 20 [max_uses=50]

    Optional `max_uses=N` caps total redemptions (default = unlimited).
    Codes are stored upper-case for case-insensitive matching.
    """
    if not is_admin(message.from_user.id):
        await message.answer("\u274c Доступ запрещён.")
        return

    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.answer(
            "<b>Использование:</b>\n"
            "<code>/promo_create CODE bonus_days N [max_uses=N]</code>\n"
            "<code>/promo_create CODE discount_pct N [max_uses=N]</code>"
        )
        return

    _, code, kind, value_raw, *opts = parts
    code = code.upper()

    if kind not in PROMO_KINDS:
        await message.answer(
            f"Тип должен быть один из: {', '.join(PROMO_KINDS)}"
        )
        return
    try:
        value = int(value_raw)
    except ValueError:
        await message.answer("Значение должно быть целым числом.")
        return
    if kind == PROMO_KIND_DISCOUNT_PCT and not 1 <= value <= 100:
        await message.answer("discount_pct должен быть в диапазоне 1..100.")
        return
    if kind == PROMO_KIND_BONUS_DAYS and value <= 0:
        await message.answer("bonus_days должен быть > 0.")
        return

    max_uses: int | None = None
    for opt in opts:
        if opt.startswith("max_uses="):
            try:
                max_uses = int(opt.split("=", 1)[1])
                if max_uses <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("max_uses должен быть положительным числом.")
                return

    try:
        new_id = await create_promo_code(
            code, kind, value,
            max_uses=max_uses,
            created_by=message.from_user.id,
        )
    except Exception as exc:
        # UNIQUE collision is the most likely path here.
        logger.warning("create_promo_code failed: %s", exc)
        await message.answer(
            "Не удалось создать промокод (возможно, уже существует)."
        )
        return

    await message.answer(
        f"\u2705 Промокод <code>{code}</code> создан "
        f"(id={new_id}, kind={kind}, value={value}, "
        f"max_uses={max_uses if max_uses else '∞'})."
    )


@router.message(Command("users"))
async def cmd_users_list(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("\u274c Доступ запрещён.")
        return

    users = await get_all_active_users()
    if not users:
        await message.answer("Нет зарегистрированных пользователей.")
        return

    lines = ["\U0001f465 <b>Пользователи:</b>\n"]
    for i, u in enumerate(users[:ADMIN_USERS_LIST_LIMIT], 1):
        username = f"@{u['username']}" if u["username"] else "без юзернейма"
        lines.append(
            f"{i}. {html_escape(u['full_name'])} "
            f"({html_escape(username)}) — ID: {u['user_id']}"
        )

    if len(users) > ADMIN_USERS_LIST_LIMIT:
        lines.append(
            f"\n... и ещё {len(users) - ADMIN_USERS_LIST_LIMIT} пользователей"
        )

    await message.answer("\n".join(lines))
