import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from bot.constants import (
    LEGACY_SUBSCRIPTION_TIER,
    PARSE_INTERVAL_DEFAULT_MINUTES,
    TIER_BASIC,
    TIER_MAX,
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


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=list)

    yookassa_shop_id: str = os.getenv("YOOKASSA_SHOP_ID", "")
    yookassa_secret_key: str = os.getenv("YOOKASSA_SECRET_KEY", "")
    yookassa_return_url: str = os.getenv("YOOKASSA_RETURN_URL", "")

    # Per-tier monthly/yearly prices (in roubles). Loaded in __post_init__ so
    # we can validate them in one place.
    tier_prices_monthly: dict[str, int] = field(default_factory=dict)
    tier_prices_yearly: dict[str, int] = field(default_factory=dict)
    # Per-tier monthly/yearly prices in Telegram Stars (XTR currency).
    tier_stars_monthly: dict[str, int] = field(default_factory=dict)
    tier_stars_yearly: dict[str, int] = field(default_factory=dict)
    # Whether the bot offers Telegram Stars as an additional payment method.
    stars_enabled: bool = os.getenv("STARS_ENABLED", "true").lower() in ("1", "true", "yes", "on")

    trial_days: int = _env_int("TRIAL_DAYS", TRIAL_DAYS_DEFAULT)
    trial_tier: str = _env_str("TRIAL_TIER", TRIAL_TIER_DEFAULT)
    legacy_subscription_tier: str = _env_str(
        "LEGACY_SUBSCRIPTION_TIER", LEGACY_SUBSCRIPTION_TIER
    )

    parse_interval: int = _env_int("PARSE_INTERVAL", PARSE_INTERVAL_DEFAULT_MINUTES)
    database_path: str = os.getenv("DATABASE_PATH", "data/bot.db")

    bot_username: str = os.getenv("BOT_USERNAME", "")
    support_username: str = os.getenv("SUPPORT_USERNAME", "")

    webhook_enabled: bool = os.getenv("WEBHOOK_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    webhook_host: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    webhook_port: int = _env_int("WEBHOOK_PORT", 8080)
    # IP allowlist for the YooKassa webhook (defence-in-depth on top of the
    # re-fetch-by-id check). Empty/unset disables the check; the literal
    # string "default" means "use the published YooKassa range" (see
    # bot/payments/yookassa_ips.py). Otherwise a comma-separated CIDR list.
    yookassa_webhook_ip_allowlist: str = os.getenv("YOOKASSA_WEBHOOK_IP_ALLOWLIST", "")
    # When the bot sits behind a reverse proxy (nginx, Cloudflare, ...) the
    # remote address is the proxy. Set this to True to use the *last* IP from
    # the `X-Forwarded-For` header instead. Only enable when you actually have
    # a trusted proxy in front: otherwise clients can spoof the source IP.
    yookassa_webhook_trust_proxy: bool = os.getenv(
        "YOOKASSA_WEBHOOK_TRUST_PROXY", "false"
    ).lower() in ("1", "true", "yes", "on")

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def __post_init__(self):
        raw = os.getenv("ADMIN_IDS", "")
        self.admin_ids = [
            int(x.strip())
            for x in raw.split(",")
            if x.strip().lstrip("-").isdigit()
        ]

        # Per-tier prices: e.g. SUBSCRIPTION_PRICE_BASIC_MONTHLY=299
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

    # Backwards-compatible aliases for code that still asks for "the" monthly /
    # yearly price (e.g. legacy help text). They resolve to Basic tier.
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
        """Per-tier price in Telegram Stars (XTR). Trial has no Stars price."""
        if tier not in TIERS:
            raise ConfigError(f"Unknown tier: {tier!r}")
        if plan == "monthly":
            return self.tier_stars_monthly[tier]
        if plan == "yearly":
            return self.tier_stars_yearly[tier]
        raise ConfigError(f"Unknown plan: {plan!r}")

    def yearly_savings_pct(self, tier: str) -> int:
        """Return the % discount of a yearly plan over 12× monthly, rounded.

        Used in marketing copy: a user paying yearly for `tier` saves this %
        compared to paying month-by-month for the same tier.
        """
        monthly_total = 12 * self.price_for(tier, "monthly")
        yearly = self.price_for(tier, "yearly")
        if monthly_total <= 0:
            return 0
        return round((1 - yearly / monthly_total) * 100)

    def validate(self, *, require_payments: bool = True) -> None:
        """Fail fast at startup if mandatory configuration is missing."""
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
        if self.trial_days < 0:
            raise ConfigError("TRIAL_DAYS must be >= 0")
        if self.trial_tier not in TIERS:
            raise ConfigError(
                f"TRIAL_TIER must be one of {TIERS}, got {self.trial_tier!r}"
            )
        if self.legacy_subscription_tier not in TIERS:
            raise ConfigError(
                f"LEGACY_SUBSCRIPTION_TIER must be one of {TIERS}, "
                f"got {self.legacy_subscription_tier!r}"
            )
        if self.parse_interval <= 0:
            raise ConfigError("PARSE_INTERVAL must be > 0")


config = Config()


# Re-exports so other modules can pick the canonical names without importing
# from `constants` and `config` separately.
__all__ = [
    "Config",
    "ConfigError",
    "TIER_BASIC",
    "TIER_MAX",
    "TIER_PRO",
    "config",
]
