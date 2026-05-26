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
from bot.handlers.orders import format_order
from bot.parsers.base import BaseParser
from bot.parsers.fl_ru import FLParser
from bot.parsers.habr_freelance import FreelanceRuParser
from bot.parsers.kwork import KworkParser
from bot.parsers.weblancer import WeblancerParser
from bot.parsers.youdo import YouDoParser

logger = logging.getLogger(__name__)


# Tracks consecutive zero-result ticks per platform. After
# `PARSER_ZERO_RESULTS_ALERT_STREAK` empty ticks in a row we ping admins so a
# silently-broken parser is caught early. The streak is reset both when the
# parser produces results and right after we alert (to avoid notification spam).
_parser_zero_streak: dict[str, int] = {}

# Day-bucket of the last reminder sweep so we don't run that scan on every
# scheduler tick (3-min cadence) — once per UTC day is enough.
_last_reminder_sweep_day: str | None = None


def _build_parsers() -> list[BaseParser]:
    return [
        KworkParser(),
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
    """Best-effort fan-out to every configured admin id."""
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, disable_web_page_preview=True)
        except Exception:
            logger.exception("Failed to send admin alert to %s", admin_id)


async def parse_all_platforms(parsers: list[BaseParser], bot: Bot | None = None) -> int:
    """Run all parsers in parallel, persist results, and emit a
    zero-results alert if a parser keeps returning empty results.
    """
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
                f"\u26a0\ufe0f <b>Парсер `{platform}`</b> не возвращает заказов "
                f"уже {streak} тиков подряд. Проверь источник.",
            )
            # Reset to 0 so we don't re-alert on every tick — re-arms only
            # after the parser produces something again, then breaks again.
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
    """Send with FloodWait retry. Returns True if delivered."""
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
                        chat_id=user_id,
                        text=plain,
                        parse_mode=None,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup,
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
    """Return how many seconds have passed since `iso_timestamp` (UTC).

    Returns infinity (i.e. "long ago") when the timestamp is missing so a
    brand-new subscription always receives its first batch.
    """
    if not iso_timestamp:
        return float("inf")
    try:
        # SQLite's `datetime('now')` returns a naive UTC string like
        # "2024-05-09 19:30:00". Treat it as UTC.
        ts = datetime.fromisoformat(iso_timestamp.replace(" ", "T"))
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _upsell_kb() -> InlineKeyboardMarkup:
    """Inline keyboard for the post-limit upsell message: jumps straight into
    the Pro / Max payment flow."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4ca Перейти на Про — мес", callback_data="pay:pro:monthly")],
        [InlineKeyboardButton(text="\U0001f525 Макс — без лимита", callback_data="pay:max:monthly")],
        [InlineKeyboardButton(text="\u2b05 Все тарифы", callback_data="subscription")],
    ])


def _upsell_text(remaining: int) -> str:
    return (
        f"\u26a1 <b>Хочешь больше заказов?</b>\n\n"
        f"Тариф {TIER_LABEL[TIER_BASIC]} ограничен {TIER_ORDERS_PER_TICK[TIER_BASIC]} "
        f"заказами за раз. Сейчас в очереди ещё <b>~{remaining}</b> подходящих "
        f"заказов, которые мы тебе не успеваем доставить.\n\n"
        f"\U0001f535 <b>Про</b> — до {TIER_ORDERS_PER_TICK['pro']} заказов "
        f"и доставка в 2 раза чаще.\n"
        f"\U0001f525 <b>Макс</b> — без лимита."
    )


async def _maybe_upsell_basic(
    bot: Bot,
    user: dict,
    delivered: int,
    last_global_ref: list[float],
) -> None:
    """If a Basic user just hit their per-tick cap and there are *more*
    matching orders waiting, send a one-shot upsell message — throttled
    to once per 24h.
    """
    tier = user.get("tier") or TIER_BASIC
    if tier != TIER_BASIC:
        return
    per_tick = TIER_ORDERS_PER_TICK[TIER_BASIC]
    if delivered < per_tick:
        return
    sub_id = user.get("subscription_id")
    if not sub_id:
        return
    if not await can_send_upsell(sub_id):
        return
    remaining = await count_remaining_unsent_for_user(user["user_id"])
    if remaining <= 0:
        return

    # Pace the upsell against the same global broadcast budget.
    elapsed = time.monotonic() - last_global_ref[0]
    if elapsed < GLOBAL_BROADCAST_INTERVAL_SECONDS:
        await asyncio.sleep(GLOBAL_BROADCAST_INTERVAL_SECONDS - elapsed)
    sent = await _send_with_retry(
        bot,
        user["user_id"],
        _upsell_text(remaining),
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
            # Graceful shutdown: stop iterating users but keep the per-user
            # batch atomic-ish (we already mark before sending).
            break
        user_id = user["user_id"]
        tier = user.get("tier") or TIER_BASIC
        cooldown = TIER_DELIVERY_COOLDOWN_SECONDS.get(
            tier, TIER_DELIVERY_COOLDOWN_SECONDS[TIER_BASIC]
        )
        # Skip users whose tier cooldown hasn't elapsed since their last batch.
        elapsed_since_last = _seconds_since(user.get("last_delivery_at"))
        if elapsed_since_last < cooldown:
            continue

        per_tick = TIER_ORDERS_PER_TICK.get(
            tier, TIER_ORDERS_PER_TICK[TIER_BASIC]
        )
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
            # Mark BEFORE sending so a Telegram failure can't cause a duplicate
            # next tick. Worst case the user misses a single order if the
            # network drops mid-send — that's strictly better than spamming.
            await mark_order_sent(user_id, order["id"])

            # Pace globally to stay below Telegram's ~30 msg/sec ceiling.
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
                # Either user blocked us or a hard error: stop sending more
                # orders to this user this tick.
                break

            # Per-chat 1 msg/sec cap.
            await asyncio.sleep(PER_CHAT_MIN_INTERVAL_SECONDS)

        if delivered_any and user.get("subscription_id"):
            await mark_subscription_delivered(user["subscription_id"])

        # If a Basic user just hit their cap and more orders are waiting,
        # nudge them toward Pro/Max — once per 24h.
        await _maybe_upsell_basic(bot, user, delivered_count, last_global_ref)

    return total_sent


async def _send_expiry_reminders(bot: Bot) -> None:
    """Send 'your subscription expires soon' nudges to paying users in the
    3-day and 1-day windows. Each window fires at most once per subscription."""
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
            if days == 1:
                head = "\u23f0 <b>Подписка заканчивается завтра!</b>"
            else:
                head = f"\u23f3 <b>Подписка заканчивается через {days} дня</b>"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"\U0001f504 Продлить {tier_name}",
                    callback_data=f"pay:{tier}:monthly",
                )],
                [InlineKeyboardButton(text="\u2b05 Подписка", callback_data="subscription")],
            ])
            text = (
                f"{head}\n\n"
                f"Чтобы заказы продолжали приходить, продли подписку в один клик."
            )
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
    """Run jobs that should fire ~once per day (independently of the parse
    cadence). Today this is just the expiry-reminders sweep."""
    global _last_reminder_sweep_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_reminder_sweep_day == today:
        return
    _last_reminder_sweep_day = today
    await _send_expiry_reminders(bot)


async def scheduler_loop(bot: Bot, stop_event: asyncio.Event | None = None) -> None:
    """Main scheduler loop.

    Cooperates with `stop_event` to shut down gracefully: it finishes the
    current parse + delivery cycle and only THEN exits. Callers should
    set `stop_event` and `await` this coroutine — it will return cleanly.
    """
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

            # Sleep in small chunks so the stop_event can interrupt us
            # without firing CancelledError mid-await.
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
