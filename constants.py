"""Project-wide constants. Centralised here to avoid magic numbers spread
across the codebase (admin lists, message truncation, scheduler limits, ...).
"""

# Telegram message limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
# Margin we reserve for the surrounding template in `format_order` so the
# overall message never exceeds Telegram's hard limit.
ORDER_MESSAGE_MAX_LENGTH = 4000

# Order rendering
ORDER_DESCRIPTION_PREVIEW = 200
ORDER_DESCRIPTION_STORED = 500

# Telegram custom emoji ("icons") rendered via <tg-emoji emoji-id="...">FALLBACK</tg-emoji>
# in the order card. Each role has its own ID so they can be tuned independently;
# users without Telegram Premium see the FALLBACK character instead of the icon.
# Set an ID to None to fall back to a plain Unicode character.
ORDER_ICON_PLATFORM_ID: str | None = "5277068107171495909"
ORDER_ICON_TITLE_ID: str | None = "5277068107171495909"
ORDER_ICON_CATEGORY_ID: str | None = "5277068107171495909"
ORDER_ICON_LINK_ID: str | None = "5277068107171495909"
# When a parser cannot derive an external_id from the URL it falls back to a
# slice of the title. We bound that slice so the column doesn't grow without
# limits.
EXT_ID_FALLBACK_LIMIT = 50

# ── Subscription tiers ──
# A "tier" controls *what* a user gets (orders per tick, delivery cooldown);
# a "plan" (monthly/yearly) controls *how long* the subscription lasts.
# Trial is its own plan but uses the tier configured by `TRIAL_TIER`.

TIER_BASIC = "basic"
TIER_PRO = "pro"
TIER_MAX = "max"
TIERS = (TIER_BASIC, TIER_PRO, TIER_MAX)

# Visual labels (kept short — these go into inline button captions).
TIER_LABEL = {
    TIER_BASIC: "Обычный",
    TIER_PRO: "Про",
    TIER_MAX: "Макс",
}
TIER_EMOJI = {
    TIER_BASIC: "\u26aa",        # ⚪
    TIER_PRO: "\U0001f535",       # 🔵
    TIER_MAX: "\U0001f525",       # 🔥
}

# Premium custom-emoji IDs for the tier marker (Telegram Premium clients render
# the icon, others see TIER_EMOJI as fallback). Inline-keyboard buttons fall
# back to plain Unicode because Telegram does not render <tg-emoji> there.
TIER_CUSTOM_EMOJI_ID = {
    TIER_BASIC: "5277068107171495909",
    TIER_PRO: "5277068107171495909",
    TIER_MAX: "5277068107171495909",
}

# Marketing badges shown next to a tier in inline buttons / subscription cards
# to nudge users towards the "intended" plan. Stored as (emoji, label) so the
# emoji can be wrapped in a custom-emoji tag inside message bodies.
TIER_BADGE_EMOJI = {
    TIER_BASIC: "",
    TIER_PRO: "\U0001f31f",      # 🌟
    TIER_MAX: "\U0001f451",      # 👑
}
TIER_BADGE_LABEL = {
    TIER_BASIC: "",
    TIER_PRO: "Хит",
    TIER_MAX: "All-In",
}
TIER_BADGE_CUSTOM_EMOJI_ID = {
    TIER_BASIC: None,
    TIER_PRO: "5277068107171495909",
    TIER_MAX: "5277068107171495909",
}

# Premium custom-emoji IDs for decorative section icons used in subscription
# cards / help messages. Set to None to fall back to the Unicode glyph.
SECTION_ICON_TARIFFS_ID: str | None = "5277068107171495909"        # 💰
SECTION_ICON_SUBSCRIPTION_ID: str | None = "5277068107171495909"   # ⭐
SECTION_ICON_LIGHTNING_ID: str | None = "5277068107171495909"      # ⚡
SECTION_ICON_TRIAL_ID: str | None = "5277068107171495909"          # 🎁
SECTION_ICON_MONTHLY_ID: str | None = "5277068107171495909"        # 📅
SECTION_ICON_YEARLY_ID: str | None = "5277068107171495909"         # 📆
SECTION_ICON_NO_SUB_ID: str | None = "5277068107171495909"         # ❌

# Short marketing tagline rendered under a tier in the subscription card.
TIER_TAGLINE = {
    TIER_BASIC: "Минимум для пробы.",
    TIER_PRO: "Лучшее соотношение цены и объёма.",
    TIER_MAX: "Максимум — для агентств и охотников.",
}

# ── Per-tier delivery speed ──────────────────────────────────────────────────
#
# `orders_per_tick`        — max orders pushed per scheduler iteration.
# `delivery_cooldown`      — min seconds between two batches to same user.
#
# New values (vs old):
#   Basic:  10 orders / 3 min   (was 5 / 6 min)  — 2× faster, 2× more
#   Pro:    30 orders / 1 min   (was 15 / 3 min)  — 3× faster, 2× more
#   Max:   100 orders / 30 sec  (was 50 / 3 min)  — 6× faster, 2× more
#
# These values can be overridden per-environment in .env:
#   ORDERS_PER_TICK_BASIC=10
#   DELIVERY_COOLDOWN_BASIC=180
#   ... etc.

TIER_ORDERS_PER_TICK = {
    TIER_BASIC: 10,
    TIER_PRO: 30,
    TIER_MAX: 100,   # safety cap; effectively unlimited for real usage
}

TIER_DELIVERY_COOLDOWN_SECONDS = {
    TIER_BASIC: 3 * 60,    # 3 min
    TIER_PRO: 1 * 60,      # 1 min
    TIER_MAX: 30,           # 30 sec
}

# Default prices in roubles for monthly/yearly subscriptions per tier.
# These can be overridden per-tier in `.env`.
TIER_PRICE_MONTHLY_DEFAULTS = {
    TIER_BASIC: 299,
    TIER_PRO: 599,
    TIER_MAX: 1499,
}
TIER_PRICE_YEARLY_DEFAULTS = {
    TIER_BASIC: 2499,
    TIER_PRO: 4999,
    TIER_MAX: 11999,
}

# How many days of free trial a new user receives, what tier they get during
# the trial, and which tier existing subscriptions are migrated to when the
# tier column was added.
TRIAL_DAYS_DEFAULT = 3
TRIAL_TIER_DEFAULT = TIER_PRO
LEGACY_SUBSCRIPTION_TIER = TIER_BASIC

# Scheduler — global parse cadence (in minutes). The scheduler ticks at this
# rate; per-tier `delivery_cooldown` decides whether a given user actually
# receives a batch on each tick.
PARSE_INTERVAL_DEFAULT_MINUTES = 3

# ── Referrals ──
REFERRAL_REFERRER_BONUS_DAYS = 14
REFERRAL_INVITED_TRIAL_BONUS_DAYS = 7
REFERRAL_CODE_LENGTH = 7

# ── Promo codes ──
PROMO_KIND_DISCOUNT_PCT = "discount_pct"
PROMO_KIND_BONUS_DAYS = "bonus_days"
PROMO_KINDS = (PROMO_KIND_DISCOUNT_PCT, PROMO_KIND_BONUS_DAYS)
PROMO_CODE_MIN_LENGTH = 3
PROMO_CODE_MAX_LENGTH = 32

# ── Telegram Stars ──
TIER_STARS_MONTHLY_DEFAULTS = {
    TIER_BASIC: 250,
    TIER_PRO: 500,
    TIER_MAX: 1250,
}
TIER_STARS_YEARLY_DEFAULTS = {
    TIER_BASIC: 2100,
    TIER_PRO: 4200,
    TIER_MAX: 10000,
}

# ── Admin tools ──
ADMIN_BROADCAST_PER_USER_DELAY_SECONDS = 0.05
ADMIN_BATCH_SIZE = 500
ADMIN_USERS_LIST_LIMIT = 50
LATEST_ORDERS_DISPLAY_LIMIT = 10

# Telegram rate limits
PER_CHAT_MIN_INTERVAL_SECONDS = 1.0
GLOBAL_BROADCAST_INTERVAL_SECONDS = 0.05

# Number of consecutive zero-result ticks before alerting admins.
PARSER_ZERO_RESULTS_ALERT_STREAK = 3

# HTTP / parsers
HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE = 1.5

# YooKassa webhook server
WEBHOOK_DEFAULT_HOST = "0.0.0.0"
WEBHOOK_DEFAULT_PORT = 8080
WEBHOOK_PATH = "/yookassa/webhook"

# ── Platforms registry ───────────────────────────────────────────────────────
#
# PLATFORM_CODE    — short snake_case key stored in DB and used in callbacks.
# PLATFORM_NAME    — human-readable display name sent to Telegram.
# PLATFORM_EMOJI   — coloured circle for quick visual identification.
#
# When adding a new platform:
#   1. Add entry here.
#   2. Add parser in bot/parsers/<code>.py.
#   3. Add to _build_parsers() in bot/scheduler.py.
#   4. Add to PLATFORMS list in bot/keyboards.py.
#   5. Add to PLATFORM_CODE_TO_NAME in bot/database.py.

PLATFORM_REGISTRY: dict[str, dict] = {
    "kwork": {
        "name": "Kwork",
        "emoji": "🟢",
        "url": "https://kwork.ru",
        "notes": "RSS feed",
    },
    "fl_ru": {
        "name": "FL.ru",
        "emoji": "🔵",
        "url": "https://www.fl.ru",
        "notes": "HTML scraper",
    },
    "freelance_ru": {
        "name": "Freelance.ru",
        "emoji": "🟠",
        "url": "https://freelance.ru",
        "notes": "HTML scraper",
    },
    "weblancer": {
        "name": "Weblancer",
        "emoji": "🔴",
        "url": "https://www.weblancer.net",
        "notes": "HTML scraper",
    },
    "youdo": {
        "name": "YouDo",
        "emoji": "🟡",
        "url": "https://youdo.com",
        "notes": "HTML scraper; may be blocked by ServicePipe",
    },
    "freelancehunt": {
        "name": "Freelancehunt",
        "emoji": "🟣",
        "url": "https://freelancehunt.com",
        "notes": "RSS primary, HTML fallback",
    },
    "habr_career": {
        "name": "Habr Career",
        "emoji": "⚫",
        "url": "https://career.habr.com/freelance",
        "notes": "RSS feed",
    },
    "upwork": {
        "name": "Upwork",
        "emoji": "🌐",
        "url": "https://www.upwork.com",
        "notes": "RSS per-category; international, English titles",
    },
}
