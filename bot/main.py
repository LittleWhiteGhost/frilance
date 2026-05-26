"""Application entrypoint.

Wires together: config validation, database lifecycle, dispatcher / handlers,
scheduler, and the YooKassa webhook server.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import ConfigError, config
from bot.database import close_db, init_db
from bot.handlers.admin import router as admin_router
from bot.handlers.categories import router as categories_router
from bot.handlers.orders import router as orders_router
from bot.handlers.payment import router as payment_router
from bot.handlers.promo import router as promo_router
from bot.handlers.start import router as start_router
from bot.handlers.subscription import router as subscription_router
from bot.payments.webhook import WebhookServer
from bot.scheduler import scheduler_loop

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, (config.log_level or "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )


async def main() -> None:
    _configure_logging()
    logger.info("Starting FreelanceParser Bot...")

    try:
        config.validate(require_payments=False)
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        raise SystemExit(2) from exc

    await init_db()
    logger.info("Database initialized")

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())

    for router in (
        start_router,
        categories_router,
        subscription_router,
        orders_router,
        payment_router,
        promo_router,
        admin_router,
    ):
        dp.include_router(router)

    # Stop event drives a *cooperative* shutdown: when SIGINT/SIGTERM arrives
    # we set it and let the scheduler finish its current delivery batch
    # before exiting, instead of yanking it via task.cancel() mid-send.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows / restricted environments — fall back to KeyboardInterrupt.
            pass

    scheduler_task = asyncio.create_task(
        scheduler_loop(bot, stop_event=stop_event), name="scheduler-loop"
    )
    webhook_server: WebhookServer | None = None
    if config.webhook_enabled:
        webhook_server = WebhookServer()
        await webhook_server.start()

    polling_task = asyncio.create_task(dp.start_polling(bot), name="aiogram-polling")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")

    logger.info("Bot is running!")
    done, pending = await asyncio.wait(
        {polling_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    logger.info("Shutdown signal received — finishing in-flight work...")
    await dp.stop_polling()
    for task in pending:
        # Don't cancel the stop_task — it's just waiting on stop_event and
        # has nothing to clean up; cancelling it raises CancelledError.
        if task is stop_task:
            continue
        task.cancel()

    # Let the scheduler observe stop_event and unwind cleanly. We give it a
    # bounded grace period; if it's still busy after that, *then* we cancel.
    try:
        await asyncio.wait_for(scheduler_task, timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Scheduler didn't finish in 30s — cancelling")
        scheduler_task.cancel()
        try:
            await scheduler_task
        except (asyncio.CancelledError, Exception):
            pass
    except (asyncio.CancelledError, Exception):
        pass

    for task in done | pending:
        if task is stop_task or task is scheduler_task:
            continue
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    if webhook_server is not None:
        await webhook_server.stop()

    await bot.session.close()
    await close_db()
    logger.info("Bye.")


if __name__ == "__main__":
    asyncio.run(main())
