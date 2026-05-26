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

# Per-tier order-delivery quotas. `orders_per_tick` is the max number of new
# orders forwarded to a single user during one scheduler iteration.
TIER_ORDERS_PER_TICK = {
    TIER_BASIC: 5,
    TIER_PRO: 15,
    TIER_MAX: 50,   # effectively unlimited; safety cap to avoid rate-limit storms
}

# Per-tier minimum interval between two delivery batches to the same user.
# Higher tiers get fresher orders; basic users get them in slower batches.
TIER_DELIVERY_COOLDOWN_SECONDS = {
    TIER_BASIC: 6 * 60,
    TIER_PRO: 3 * 60,
    TIER_MAX: 3 * 60,
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
# Bonus credited to the *referrer* when a referred user makes their first paid
# subscription. The referred user gets `REFERRAL_INVITED_TRIAL_BONUS_DAYS`
# extra days appended to their trial when they /start with a ref link.
REFERRAL_REFERRER_BONUS_DAYS = 14
REFERRAL_INVITED_TRIAL_BONUS_DAYS = 7
# Length of the random referral code — short enough to fit nicely in a t.me
# link and long enough to avoid collisions for >>1M users.
REFERRAL_CODE_LENGTH = 7

# ── Promo codes ──
PROMO_KIND_DISCOUNT_PCT = "discount_pct"   # 0..100
PROMO_KIND_BONUS_DAYS = "bonus_days"       # any positive integer
PROMO_KINDS = (PROMO_KIND_DISCOUNT_PCT, PROMO_KIND_BONUS_DAYS)
PROMO_CODE_MIN_LENGTH = 3
PROMO_CODE_MAX_LENGTH = 32

# ── Telegram Stars ──
# Stars are an in-Telegram currency (`XTR`). Conversion is roughly 1 Star ≈
# 0.013 USD ≈ 1.2 RUB but rates fluctuate; we publish a static table per tier
# rather than computing on-the-fly from RUB so prices stay round.
# Override via `.env` (STARS_PRICE_<TIER>_<PLAN>=NN).
TIER_STARS_MONTHLY_DEFAULTS = {
    TIER_BASIC: 250,    # ~299 ₽
    TIER_PRO: 500,      # ~599 ₽
    TIER_MAX: 1250,     # ~1499 ₽
}
TIER_STARS_YEARLY_DEFAULTS = {
    TIER_BASIC: 2100,   # ~2499 ₽
    TIER_PRO: 4200,     # ~4999 ₽
    TIER_MAX: 10000,    # ~11999 ₽
}

# ── Admin tools ──
# Default throttle (seconds) between broadcasted messages. We stay under
# Telegram's ~30 msg/sec global cap with a generous safety margin.
ADMIN_BROADCAST_PER_USER_DELAY_SECONDS = 0.05
# How many user_ids we feed to one batch insert in admin operations.
ADMIN_BATCH_SIZE = 500

# Admin
ADMIN_USERS_LIST_LIMIT = 50
LATEST_ORDERS_DISPLAY_LIMIT = 10

# Telegram rate limits — Bot API allows ~1 msg/sec per chat and ~30 msg/sec
# globally. We pace ourselves to stay well clear of those ceilings.
PER_CHAT_MIN_INTERVAL_SECONDS = 1.0
GLOBAL_BROADCAST_INTERVAL_SECONDS = 0.05

# Number of consecutive scheduler ticks a parser may return zero results
# before we alert admins. Set this above 1 so a transient empty page (RSS
# briefly empty after a deploy/restart on the source's side) doesn't page us.
PARSER_ZERO_RESULTS_ALERT_STREAK = 3

# HTTP / parsers
HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE = 1.5

# YooKassa webhook server
WEBHOOK_DEFAULT_HOST = "0.0.0.0"
WEBHOOK_DEFAULT_PORT = 8080
WEBHOOK_PATH = "/yookassa/webhook"
