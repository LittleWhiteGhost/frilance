"""HTTP webhook server for YooKassa `payment.succeeded` notifications.

YooKassa pushes notifications to a configured URL when a payment changes
state. To verify these are genuinely from YooKassa, we re-fetch the payment
by id via the SDK (the public IP allowlist + this server-side re-fetch is
what YooKassa recommends in their docs as the simplest robust verification —
they do not currently sign the webhook body).
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

from bot.config import config
from bot.constants import (
    TIERS,
    WEBHOOK_DEFAULT_HOST,
    WEBHOOK_DEFAULT_PORT,
    WEBHOOK_PATH,
)
from bot.database import (
    activate_subscription,
    get_payment,
    update_payment_status,
)
from bot.payments.amounts import amount_matches
from bot.payments.yookassa import check_payment, expected_amount
from bot.payments.yookassa_ips import ip_is_allowed, parse_allowlist

logger = logging.getLogger(__name__)


def _client_ip(request: web.Request) -> str | None:
    """Resolve the source IP of `request`.

    With `YOOKASSA_WEBHOOK_TRUST_PROXY=true` we trust a reverse proxy in front
    of us to populate `X-Forwarded-For` and take the LEFT-MOST entry — that's
    the original client (YooKassa) as the proxy saw it. Without that flag we
    fall back to the raw socket peer, which is correct when YooKassa hits us
    directly with no proxy.

    Only enable the proxy flag when you actually have a trusted proxy in
    front: otherwise any external client can spoof `X-Forwarded-For` to
    bypass the allowlist.
    """
    if config.yookassa_webhook_trust_proxy:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[0]
    return request.remote or None


async def _handle_yookassa_webhook(request: web.Request) -> web.Response:
    # IP allowlist (defence-in-depth). Disabled when the env var is empty.
    allowlist = parse_allowlist(config.yookassa_webhook_ip_allowlist)
    if allowlist:
        src = _client_ip(request)
        if not src or not ip_is_allowed(src, allowlist):
            logger.warning(
                "Rejecting webhook from non-allowlisted IP %s", src,
            )
            return web.Response(status=403, text="forbidden")

    try:
        body = await request.json()
    except Exception:
        logger.warning("Invalid JSON in webhook body")
        return web.Response(status=400, text="invalid json")

    event = body.get("event")
    obj = body.get("object") or {}
    payment_id = obj.get("id")

    if not payment_id:
        logger.warning("Webhook without payment id: %s", body)
        return web.Response(status=400, text="missing payment id")

    # Re-fetch from YooKassa to authenticate the notification.
    fetched = check_payment(payment_id)
    if not fetched:
        logger.warning("Could not re-fetch payment %s from YooKassa", payment_id)
        return web.Response(status=200, text="ok")

    # Look up our local record to know which user/tier/plan this belongs to.
    local = await get_payment(payment_id)
    if not local:
        logger.warning("Webhook for unknown payment_id=%s (event=%s)", payment_id, event)
        return web.Response(status=200, text="ok")

    tier = local.get("tier")
    plan = local.get("plan")
    if tier not in TIERS or plan not in ("monthly", "yearly"):
        logger.warning(
            "Webhook for payment %s with bad (tier=%s, plan=%s)",
            payment_id, tier, plan,
        )
        return web.Response(status=200, text="ok")

    if event == "payment.succeeded" or fetched["status"] == "succeeded":
        # Validate the amount against what we stored when the invoice was
        # created (i.e. catalogue price minus any applied promo discount).
        # Using the catalogue price directly here would falsely reject
        # legitimate discounted payments.
        expected_paid = local["amount"]
        if not amount_matches(fetched["amount"], expected_paid):
            logger.error(
                "Webhook amount mismatch for %s: got %s, expected %s "
                "(tier=%s, plan=%s)",
                payment_id, fetched["amount"], expected_paid, tier, plan,
            )
            await update_payment_status(payment_id, "amount_mismatch")
            return web.Response(status=200, text="ok")
        if local["status"] == "succeeded":
            return web.Response(status=200, text="ok")
        await update_payment_status(payment_id, "succeeded")
        await activate_subscription(local["user_id"], tier, plan, payment_id)
        logger.info(
            "Webhook activated subscription user=%s tier=%s plan=%s payment=%s",
            local["user_id"], tier, plan, payment_id,
        )
    elif event in ("payment.canceled", "payment.waiting_for_capture"):
        await update_payment_status(payment_id, fetched["status"])
    return web.Response(status=200, text="ok")


def _amount_matches(value: object, tier: str, plan: str) -> bool:
    return amount_matches(value, expected_amount(tier, plan))


async def _handle_health(request: web.Request) -> web.Response:
    """Liveness/readiness probe.

    Returns 200 with `ok` on success, 503 with details if the database is
    unreachable. Designed for UptimeRobot / Statuscake / Kubernetes probes —
    a single GET, no auth, JSON body so a human can also read it.
    """
    # Imported here to avoid a circular import at module-load time
    # (bot.database -> bot.config -> bot.payments).
    from bot.database import db

    try:
        if db._conn is None:  # not initialised yet
            return web.json_response({"status": "starting"}, status=503)
        cur = await db.conn.execute("SELECT 1")
        await cur.fetchone()
    except Exception as exc:
        logger.exception("Health check failed")
        return web.json_response(
            {"status": "unhealthy", "error": str(exc)[:200]}, status=503
        )
    return web.json_response({"status": "ok"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, _handle_yookassa_webhook)
    # Both /health and /healthz are accepted so any uptime monitor convention
    # works out of the box.
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/healthz", _handle_health)
    return app


class WebhookServer:
    """Lifecycle helper for running the aiohttp webhook server alongside the bot."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.host = host or config.webhook_host or WEBHOOK_DEFAULT_HOST
        self.port = port if port is not None else (config.webhook_port or WEBHOOK_DEFAULT_PORT)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(
            "YooKassa webhook listening on http://%s:%s%s",
            self.host, self.port, WEBHOOK_PATH,
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
