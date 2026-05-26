"""Background scheduler.

Three responsibilities:
* Periodically run all parsers in parallel and persist new orders.
* Push the new orders out to subscribed users while respecting Telegram
  rate limits, per-tier delivery cooldowns and surviving FloodWait.
* Send marketing nudges (upsell-after-limit, expiry reminders) and
  alert admins when a parser stops returning results.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import config
from bot.constants import (
    GLOBAL_BROADCAST_INTERVAL_SECONDS,
    PARSER_ZERO_RESULTS_ALERT_STREAK,
    PER_CHAT_MIN_INTERVAL_SECONDS,
    TIER_BASIC,
    TIER_DELIVERY_COOLDOWN_SECONDS,
    TIER_LABEL,
    TIER_ORDERS_PER_TICK,
)
from bot.database import (
    can_send_upsell,
    count_remaining_unsent_for_user,
    get_subscribed_users,
    get_subscriptions_needing_reminder,
    get_unsent_orders_for_user,
    mark_order_sent,
    mark_reminder_sent,
    mark_subscription_delivered,
    mark_upsell_sent,
    mark_user_inactive,
    save_orders,
)
from bot.handlers.order_card import format_order
from bot.parsers.base import BaseParser

# ── Original parsers ─────────────────────────────────────────────────────────
from bot.parsers.fl_ru import FLParser
from bot.parsers.habr_freelance import FreelanceRuParser
from bot.parsers.kwork import KworkParser
from bot.parsers.weblancer import WeblancerParser
from bot.parsers.youdo import YouDoParser

# ── New parsers ───────────────────────────────────────────────────────────────
from bot.parsers.freelancehunt import FreelancehuntParser
from bot.parsers.habr_career import HabrCareerParser
from bot.parsers.upwork import UpworkParser

logger = logging.getLogger(__name__)

_parser_zero_streak: dict[str, int] = {}
_last_reminder_sweep_day: str | None = None


def _build_parsers() -> list[BaseParser]:
    """Return all active parser instances.

    Order matters for zero-results streak reporting (most-reliable first so
    admins can spot which specific source broke).
    """
    return [
        # RSS-based (most reliable, least likely to break on HTML changes)
        KworkParser(),
        HabrCareerParser(),
        FreelancehuntParser(),
        UpworkParser(),
        # HTML-based (may need selector updates when sites redesign)
        FLParser(),
        FreelanceRuParser(),
        WeblancerParser(),
        YouDoParser(),
    ]


async def _close_parsers(parsers: Iterable[BaseParser]) -> None:
    for parser in parsers:
        try:
            await parser.close()
        except Exception:
            logger.exception("Error closing parser %s", parser.platform_name)


async def _alert_admins(bot: Bot, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, disable_web_page_preview=True)
        except Exception:
            logger.exception("Failed to send admin alert to %s", admin_id)


async def parse_all_platforms(parsers: list[BaseParser], bot: Bot | None = None) -> int:
    tasks = [parser.safe_parse() for parser in parsers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_orders: list[dict] = []
    for parser, result in zip(parsers, results):
        platform = parser.platform_name
        if isinstance(result, list):
            all_orders.extend(o.to_dict() for o in result)
            if result:
                _parser_zero_streak[platform] = 0
            else:
                _parser_zero_streak[platform] = _parser_zero_streak.get(platform, 0) + 1
        elif isinstance(result, Exception):
            logger.error("[%s] parser crashed: %s", platform, result)
            _parser_zero_streak[platform] = _parser_zero_streak.get(platform, 0) + 1

        streak = _parser_zero_streak.get(platform, 0)
        if streak >= PARSER_ZERO_RESULTS_ALERT_STREAK and bot is not None:
            await _alert_admins(
                bot,
                f"⚠️ <b>Парсер `{platform}`</b> не возвращает заказов "
                f"уже {streak} тиков подряд. Проверь источник.",
            )
            _parser_zero_streak[platform] = 0

    new_ids = await save_orders(all_orders) if all_orders else []
    logger.info("Parsed %s total orders, %s new", len(all_orders), len(new_ids))
    return len(new_ids)


async def _send_with_retry(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    for attempt in range(3):
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            return True
        except TelegramRetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 1)) + 0.5
            logger.warning("FloodWait for %s: sleeping %.1fs", user_id, wait)
            await asyncio.sleep(wait)
        except TelegramForbiddenError:
            logger.info("User %s blocked the bot — marking inactive", user_id)
            await mark_user_inactive(user_id)
            return False
        except TelegramBadRequest as exc:
            if "DOCUMENT_INVALID" in str(exc) or "can't parse" in str(exc).lower():
                import re as _re
                plain = _re.sub(r"<[^>]+>", "", text)
                try:
                    await bot.send_message(
                        chat_id=user_id, text=plain, parse_mode=None,
                        disable_web_page_preview=True, reply_markup=reply_markup,
                    )
                    return True
                except Exception:
                    pass
            logger.exception("Failed to send order to %s (attempt %s)", user_id, attempt + 1)
            await asyncio.sleep(1)
        except Exception:
            logger.exception("Failed to send order to %s (attempt %s)", user_id, attempt + 1)
            await asyncio.sleep(1)
    return False


def _seconds_since(iso_timestamp: str | None) -> float:
    if not iso_timestamp:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_timestamp.replace(" ", "T"))
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _upsell_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Перейти на Про — мес", callback_data="pay:pro:monthly")],
        [InlineKeyboardButton(text="🔥 Макс — без лимита", callback_data="pay:max:monthly")],
        [InlineKeyboardButton(text="⬅ Все тарифы", callback_data="subscription")],
    ])


def _upsell_text(remaining: int, tier: str) -> str:
    per_tick = config.orders_per_tick(tier)
    cooldown_str = config.cooldown_label(tier)
    return (
        f"⚡ <b>Хочешь больше заказов?</b>\n\n"
        f"Тариф {TIER_LABEL[tier]} ограничен {per_tick} заказами каждые {cooldown_str}. "
        f"Сейчас в очереди ещё <b>~{remaining}</b> подходящих заказов.\n\n"
        f"🔵 <b>Про</b> — до {config.orders_per_tick('pro')} заказов каждую минуту\n"
        f"🔥 <b>Макс</b> — до {config.orders_per_tick('max')} заказов каждые 30 секунд"
    )


async def _maybe_upsell_basic(
    bot: Bot,
    user: dict,
    delivered: int,
    last_global_ref: list[float],
) -> None:
    tier = user.get("tier") or TIER_BASIC
    if tier != TIER_BASIC:
        return
    per_tick = config.orders_per_tick(TIER_BASIC)
    if delivered < per_tick:
        return
    sub_id = user.get("subscription_id")
    if not sub_id or not await can_send_upsell(sub_id):
        return
    remaining = await count_remaining_unsent_for_user(user["user_id"])
    if remaining <= 0:
        return

    elapsed = time.monotonic() - last_global_ref[0]
    if elapsed < GLOBAL_BROADCAST_INTERVAL_SECONDS:
        await asyncio.sleep(GLOBAL_BROADCAST_INTERVAL_SECONDS - elapsed)
    sent = await _send_with_retry(
        bot, user["user_id"],
        _upsell_text(remaining, TIER_BASIC),
        reply_markup=_upsell_kb(),
    )
    last_global_ref[0] = time.monotonic()
    if sent:
        await mark_upsell_sent(sub_id)


async def notify_users(bot: Bot, stop_event: asyncio.Event | None = None) -> int:
    users = await get_subscribed_users()
    total_sent = 0
    last_global_ref = [0.0]

    for user in users:
        if stop_event is not None and stop_event.is_set():
            break
        user_id = user["user_id"]
        tier = user.get("tier") or TIER_BASIC
        cooldown = config.delivery_cooldown(tier)
        if _seconds_since(user.get("last_delivery_at")) < cooldown:
            continue

        per_tick = config.orders_per_tick(tier)
        try:
            orders = await get_unsent_orders_for_user(user_id, limit=per_tick)
        except Exception:
            logger.exception("Failed to load orders for user %s", user_id)
            continue
        if not orders:
            continue

        delivered_any = False
        delivered_count = 0
        for order in orders:
            await mark_order_sent(user_id, order["id"])

            elapsed = time.monotonic() - last_global_ref[0]
            if elapsed < GLOBAL_BROADCAST_INTERVAL_SECONDS:
                await asyncio.sleep(GLOBAL_BROADCAST_INTERVAL_SECONDS - elapsed)

            sent = await _send_with_retry(bot, user_id, format_order(order))
            last_global_ref[0] = time.monotonic()
            if sent:
                total_sent += 1
                delivered_any = True
                delivered_count += 1
            else:
                break

            await asyncio.sleep(PER_CHAT_MIN_INTERVAL_SECONDS)

        if delivered_any and user.get("subscription_id"):
            await mark_subscription_delivered(user["subscription_id"])

        await _maybe_upsell_basic(bot, user, delivered_count, last_global_ref)

    return total_sent


async def _send_expiry_reminders(bot: Bot) -> None:
    for days in (3, 1):
        try:
            due = await get_subscriptions_needing_reminder(days)
        except Exception:
            logger.exception("Failed to scan reminders for %sd window", days)
            continue
        for sub in due:
            user_id = sub.get("uid") or sub.get("user_id")
            tier = sub.get("tier") or TIER_BASIC
            tier_name = TIER_LABEL.get(tier, tier)
            head = "⏰ <b>Подписка заканчивается завтра!</b>" if days == 1 else \
                   f"⏳ <b>Подписка заканчивается через {days} дня</b>"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"🔄 Продлить {tier_name}",
                    callback_data=f"pay:{tier}:monthly",
                )],
                [InlineKeyboardButton(text="⬅ Подписка", callback_data="subscription")],
            ])
            text = f"{head}\n\nЧтобы заказы продолжали приходить, продли подписку в один клик."
            try:
                await bot.send_message(
                    chat_id=user_id, text=text, reply_markup=kb,
                    disable_web_page_preview=True,
                )
                await mark_reminder_sent(sub["id"], days)
            except TelegramForbiddenError:
                await mark_user_inactive(user_id)
            except Exception:
                logger.exception("Failed to send %sd reminder to user %s", days, user_id)
            await asyncio.sleep(GLOBAL_BROADCAST_INTERVAL_SECONDS)


async def _maybe_run_daily_jobs(bot: Bot) -> None:
    global _last_reminder_sweep_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_reminder_sweep_day == today:
        return
    _last_reminder_sweep_day = today
    await _send_expiry_reminders(bot)


async def scheduler_loop(bot: Bot, stop_event: asyncio.Event | None = None) -> None:
    logger.info("Scheduler started (interval: %s min)", config.parse_interval)
    parsers = _build_parsers()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                logger.info("Starting parse cycle...")
                new_orders = await parse_all_platforms(parsers, bot=bot)
                logger.info("Parse cycle done: %s new orders", new_orders)

                if new_orders > 0:
                    sent = await notify_users(bot, stop_event=stop_event)
                    logger.info("Notifications sent: %s", sent)

                await _maybe_run_daily_jobs(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler tick failed")

            total_sleep = config.parse_interval * 60
            slept = 0.0
            while slept < total_sleep:
                if stop_event is not None and stop_event.is_set():
                    break
                step = min(1.0, total_sleep - slept)
                try:
                    await asyncio.sleep(step)
                except asyncio.CancelledError:
                    raise
                slept += step
    finally:
        await _close_parsers(parsers)
