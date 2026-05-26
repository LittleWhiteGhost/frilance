import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from bot.constants import (
    LEGACY_SUBSCRIPTION_TIER,
    PARSE_INTERVAL_DEFAULT_MINUTES,
    PLATFORM_REGISTRY,
    TIER_BASIC,
    TIER_DELIVERY_COOLDOWN_SECONDS,
    TIER_MAX,
    TIER_ORDERS_PER_TICK,
    TIER_PRICE_MONTHLY_DEFAULTS,
    TIER_PRICE_YEARLY_DEFAULTS,
    TIER_PRO,
    TIER_STARS_MONTHLY_DEFAULTS,
    TIER_STARS_YEARLY_DEFAULTS,
    TIERS,
    TRIAL_DAYS_DEFAULT,
    TRIAL_TIER_DEFAULT,
)

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None and raw != "" else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    admin_ids: list[int] = field(default_factory=list)

    yookassa_shop_id: str = field(default_factory=lambda: os.getenv("YOOKASSA_SHOP_ID", ""))
    yookassa_secret_key: str = field(default_factory=lambda: os.getenv("YOOKASSA_SECRET_KEY", ""))
    yookassa_return_url: str = field(default_factory=lambda: os.getenv("YOOKASSA_RETURN_URL", ""))

    # ── Subscription prices ──────────────────────────────────────────────────
    tier_prices_monthly: dict[str, int] = field(default_factory=dict)
    tier_prices_yearly: dict[str, int] = field(default_factory=dict)
    tier_stars_monthly: dict[str, int] = field(default_factory=dict)
    tier_stars_yearly: dict[str, int] = field(default_factory=dict)
    stars_enabled: bool = field(
        default_factory=lambda: _env_bool("STARS_ENABLED", True)
    )

    # ── Delivery speed (overridable per tier via .env) ───────────────────────
    #
    # Orders per scheduler tick:
    #   ORDERS_PER_TICK_BASIC=10   (default 10)
    #   ORDERS_PER_TICK_PRO=30     (default 30)
    #   ORDERS_PER_TICK_MAX=100    (default 100)
    #
    # Minimum seconds between batches to the same user:
    #   DELIVERY_COOLDOWN_BASIC=180   (default 180 = 3 min)
    #   DELIVERY_COOLDOWN_PRO=60      (default 60  = 1 min)
    #   DELIVERY_COOLDOWN_MAX=30      (default 30  = 30 sec)
    #
    tier_orders_per_tick: dict[str, int] = field(default_factory=dict)
    tier_delivery_cooldown: dict[str, int] = field(default_factory=dict)

    # ── Trial ────────────────────────────────────────────────────────────────
    trial_days: int = field(default_factory=lambda: _env_int("TRIAL_DAYS", TRIAL_DAYS_DEFAULT))
    trial_tier: str = field(default_factory=lambda: _env_str("TRIAL_TIER", TRIAL_TIER_DEFAULT))
    legacy_subscription_tier: str = field(
        default_factory=lambda: _env_str("LEGACY_SUBSCRIPTION_TIER", LEGACY_SUBSCRIPTION_TIER)
    )

    # ── Misc ─────────────────────────────────────────────────────────────────
    parse_interval: int = field(
        default_factory=lambda: _env_int("PARSE_INTERVAL", PARSE_INTERVAL_DEFAULT_MINUTES)
    )
    database_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "data/bot.db"))
    bot_username: str = field(default_factory=lambda: os.getenv("BOT_USERNAME", ""))
    support_username: str = field(default_factory=lambda: os.getenv("SUPPORT_USERNAME", ""))

    webhook_enabled: bool = field(
        default_factory=lambda: _env_bool("WEBHOOK_ENABLED", False)
    )
    webhook_host: str = field(default_factory=lambda: os.getenv("WEBHOOK_HOST", "0.0.0.0"))
    webhook_port: int = field(default_factory=lambda: _env_int("WEBHOOK_PORT", 8080))
    yookassa_webhook_ip_allowlist: str = field(
        default_factory=lambda: os.getenv("YOOKASSA_WEBHOOK_IP_ALLOWLIST", "")
    )
    yookassa_webhook_trust_proxy: bool = field(
        default_factory=lambda: _env_bool("YOOKASSA_WEBHOOK_TRUST_PROXY", False)
    )

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self):
        # ── Admin IDs ────────────────────────────────────────────────────────
        raw = os.getenv("ADMIN_IDS", "")
        self.admin_ids = [
            int(x.strip())
            for x in raw.split(",")
            if x.strip().lstrip("-").isdigit()
        ]

        # ── Subscription prices ──────────────────────────────────────────────
        self.tier_prices_monthly = {
            tier: _env_int(
                f"SUBSCRIPTION_PRICE_{tier.upper()}_MONTHLY",
                TIER_PRICE_MONTHLY_DEFAULTS[tier],
            )
            for tier in TIERS
        }
        self.tier_prices_yearly = {
            tier: _env_int(
                f"SUBSCRIPTION_PRICE_{tier.upper()}_YEARLY",
                TIER_PRICE_YEARLY_DEFAULTS[tier],
            )
            for tier in TIERS
        }
        self.tier_stars_monthly = {
            tier: _env_int(
                f"STARS_PRICE_{tier.upper()}_MONTHLY",
                TIER_STARS_MONTHLY_DEFAULTS[tier],
            )
            for tier in TIERS
        }
        self.tier_stars_yearly = {
            tier: _env_int(
                f"STARS_PRICE_{tier.upper()}_YEARLY",
                TIER_STARS_YEARLY_DEFAULTS[tier],
            )
            for tier in TIERS
        }

        # ── Delivery speed ───────────────────────────────────────────────────
        self.tier_orders_per_tick = {
            tier: _env_int(
                f"ORDERS_PER_TICK_{tier.upper()}",
                TIER_ORDERS_PER_TICK[tier],
            )
            for tier in TIERS
        }
        self.tier_delivery_cooldown = {
            tier: _env_int(
                f"DELIVERY_COOLDOWN_{tier.upper()}",
                TIER_DELIVERY_COOLDOWN_SECONDS[tier],
            )
            for tier in TIERS
        }

    # ── Price helpers ────────────────────────────────────────────────────────

    @property
    def subscription_price_monthly(self) -> int:
        return self.tier_prices_monthly[TIER_BASIC]

    @property
    def subscription_price_yearly(self) -> int:
        return self.tier_prices_yearly[TIER_BASIC]

    def price_for(self, tier: str, plan: str) -> int:
        if tier not in TIERS:
            raise ConfigError(f"Unknown tier: {tier!r}")
        if plan == "monthly":
            return self.tier_prices_monthly[tier]
        if plan == "yearly":
            return self.tier_prices_yearly[tier]
        raise ConfigError(f"Unknown plan: {plan!r}")

    def stars_price_for(self, tier: str, plan: str) -> int:
        if tier not in TIERS:
            raise ConfigError(f"Unknown tier: {tier!r}")
        if plan == "monthly":
            return self.tier_stars_monthly[tier]
        if plan == "yearly":
            return self.tier_stars_yearly[tier]
        raise ConfigError(f"Unknown plan: {plan!r}")

    def yearly_savings_pct(self, tier: str) -> int:
        monthly_total = 12 * self.price_for(tier, "monthly")
        yearly = self.price_for(tier, "yearly")
        if monthly_total <= 0:
            return 0
        return round((1 - yearly / monthly_total) * 100)

    # ── Delivery speed helpers ───────────────────────────────────────────────

    def orders_per_tick(self, tier: str) -> int:
        """Max orders pushed per scheduler tick for `tier`.

        Falls back to Basic if tier is unknown — safe default.
        """
        return self.tier_orders_per_tick.get(
            tier, self.tier_orders_per_tick[TIER_BASIC]
        )

    def delivery_cooldown(self, tier: str) -> int:
        """Minimum seconds between delivery batches to the same user."""
        return self.tier_delivery_cooldown.get(
            tier, self.tier_delivery_cooldown[TIER_BASIC]
        )

    def cooldown_label(self, tier: str) -> str:
        """Human-readable cooldown string for UI ('3 мин', '30 сек')."""
        secs = self.delivery_cooldown(tier)
        if secs >= 60:
            mins = secs // 60
            return f"{mins} мин"
        return f"{secs} сек"

    # ── Validation ───────────────────────────────────────────────────────────

    def validate(self, *, require_payments: bool = True) -> None:
        missing: list[str] = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if require_payments:
            if not self.yookassa_shop_id:
                missing.append("YOOKASSA_SHOP_ID")
            if not self.yookassa_secret_key:
                missing.append("YOOKASSA_SECRET_KEY")
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        for tier, price in self.tier_prices_monthly.items():
            if price <= 0:
                raise ConfigError(f"Monthly price for {tier} must be positive")
        for tier, price in self.tier_prices_yearly.items():
            if price <= 0:
                raise ConfigError(f"Yearly price for {tier} must be positive")
        for tier, val in self.tier_orders_per_tick.items():
            if val <= 0:
                raise ConfigError(f"ORDERS_PER_TICK_{tier.upper()} must be > 0")
        for tier, val in self.tier_delivery_cooldown.items():
            if val <= 0:
                raise ConfigError(f"DELIVERY_COOLDOWN_{tier.upper()} must be > 0")
        if self.trial_days < 0:
            raise ConfigError("TRIAL_DAYS must be >= 0")
        if self.trial_tier not in TIERS:
            raise ConfigError(f"TRIAL_TIER must be one of {TIERS}, got {self.trial_tier!r}")
        if self.legacy_subscription_tier not in TIERS:
            raise ConfigError(
                f"LEGACY_SUBSCRIPTION_TIER must be one of {TIERS}, "
                f"got {self.legacy_subscription_tier!r}"
            )
        if self.parse_interval <= 0:
            raise ConfigError("PARSE_INTERVAL must be > 0")


config = Config()

__all__ = ["Config", "ConfigError", "TIER_BASIC", "TIER_MAX", "TIER_PRO", "config"]
