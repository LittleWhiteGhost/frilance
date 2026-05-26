import asyncio
import logging
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from bot.config import config

logger = logging.getLogger(__name__)

DB_PATH = config.database_path

# Latest schema version. Bump this and add a migration in `_migrate` whenever
# schema changes are required.
SCHEMA_VERSION = 5


class Database:
    """Long-lived single-connection wrapper around aiosqlite.

    The previous implementation opened a new SQLite connection for every call,
    which is both slow (hundreds of microseconds per `open()` + pragma init)
    and unsafe under concurrent writers. We now keep one connection per process
    guarded by an asyncio lock for write-serialisation.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


db = Database(DB_PATH)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _get_user_version(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _set_user_version(conn: aiosqlite.Connection, version: int) -> None:
    await conn.execute(f"PRAGMA user_version = {int(version)}")


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Run forward-only migrations using SQLite's built-in `user_version`."""
    current = await _get_user_version(conn)
    if current >= SCHEMA_VERSION:
        return

    if current < 1:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                registered_at TEXT NOT NULL DEFAULT (datetime('now')),
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL DEFAULT 'trial',
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                payment_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS user_categories (
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                PRIMARY KEY (user_id, category),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS user_platforms (
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                PRIMARY KEY (user_id, platform),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                price TEXT,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(platform, external_id)
            );

            CREATE TABLE IF NOT EXISTS sent_orders (
                user_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, order_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                payment_id TEXT NOT NULL UNIQUE,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                status TEXT NOT NULL DEFAULT 'pending',
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_platform     ON orders(platform);
            CREATE INDEX IF NOT EXISTS idx_orders_created_at   ON orders(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sent_orders_user    ON sent_orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_subs_user_active    ON subscriptions(user_id, is_active, expires_at);
            CREATE INDEX IF NOT EXISTS idx_user_categories_uid ON user_categories(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_platforms_uid  ON user_platforms(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_user       ON payments(user_id);
            """
        )
        await _set_user_version(conn, 1)
        await conn.commit()

    if current < 2:
        # v2 introduces tiered subscriptions (basic/pro/max). Existing rows
        # are mapped to `LEGACY_SUBSCRIPTION_TIER` (default: basic) so we
        # don't silently upgrade paid customers to a higher tier.
        legacy_tier = config.legacy_subscription_tier
        await conn.executescript(
            f"""
            ALTER TABLE subscriptions ADD COLUMN tier TEXT NOT NULL DEFAULT '{legacy_tier}';
            ALTER TABLE subscriptions ADD COLUMN last_delivery_at TEXT;
            ALTER TABLE payments ADD COLUMN tier TEXT NOT NULL DEFAULT '{legacy_tier}';
            """
        )
        await _set_user_version(conn, 2)
        await conn.commit()

    if current < 3:
        # v3: track when we sent expiry reminders / upsell nudges so we can
        # throttle them (one 3-day reminder, one 1-day reminder, one upsell
        # per 24h).
        await conn.executescript(
            """
            ALTER TABLE subscriptions ADD COLUMN reminder_3d_sent_at TEXT;
            ALTER TABLE subscriptions ADD COLUMN reminder_1d_sent_at TEXT;
            ALTER TABLE subscriptions ADD COLUMN last_upsell_at TEXT;
            """
        )
        await _set_user_version(conn, 3)
        await conn.commit()

    if current < 4:
        # v4: referrals + promo codes + Telegram Stars support.
        # `users.referral_code` is filled lazily on first /referral request,
        # so existing rows are left NULL until the user opts in.
        await conn.executescript(
            """
            ALTER TABLE users ADD COLUMN referral_code TEXT;
            ALTER TABLE users ADD COLUMN referred_by INTEGER;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code
                ON users(referral_code) WHERE referral_code IS NOT NULL;

            -- Currency on payments was hard-coded to RUB; with Stars we now
            -- store XTR alongside RUB. `provider` distinguishes the rail.
            ALTER TABLE payments ADD COLUMN provider TEXT NOT NULL DEFAULT 'yookassa';

            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,           -- 'discount_pct' | 'bonus_days'
                value INTEGER NOT NULL,       -- 0..100 for pct; >0 for days
                max_uses INTEGER,             -- NULL = unlimited
                used_count INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,              -- NULL = never expires
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_by INTEGER,           -- admin user_id (audit only)
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);

            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL DEFAULT (datetime('now')),
                payment_id TEXT,
                UNIQUE (code_id, user_id),  -- one redemption per (code,user)
                FOREIGN KEY (code_id) REFERENCES promo_codes(id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- A bank of "free days" we owe a user. Spent at next paid activation
            -- by extending `expires_at`. Used by both the referrer-bonus and
            -- the bonus-days promo codes.
            CREATE TABLE IF NOT EXISTS bonus_days_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,        -- positive = credit; negative = debit
                source TEXT NOT NULL,         -- 'referral' | 'promo' | 'manual'
                source_ref TEXT,              -- code or referrer id
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                consumed_at TEXT,             -- NULL = still in the pot
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_bonus_days_user_unconsumed
                ON bonus_days_ledger(user_id) WHERE consumed_at IS NULL;
            """
        )
        await _set_user_version(conn, 4)
        await conn.commit()

    if current < 5:
        # v5: events table for analytics. Properties are stored as JSON for
        # flexibility — we read them rarely (admin stats) and don't need to
        # query inside them. created_at is indexed for time-window queries.
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,              -- NULL for system events
                event_type TEXT NOT NULL,
                properties TEXT,              -- JSON; NULL allowed
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_events_type_time
                ON events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_user_time
                ON events(user_id, created_at);
            """
        )
        await _set_user_version(conn, 5)
        await conn.commit()


async def init_db() -> None:
    conn = await db.connect()
    await _migrate(conn)


async def close_db() -> None:
    await db.close()


# ── User operations ──

async def add_user(
    user_id: int,
    username: str | None,
    full_name: str,
    referred_by: int | None = None,
) -> bool:
    """Insert (or refresh) a user atomically. Returns True if the user is new
    (i.e. has no subscription record yet).

    `referred_by` is honoured ONLY on the very first INSERT — it can't be
    set retroactively by re-running /start with a ref link, otherwise users
    could shop around for the friend who's online to send them the bonus.
    """
    async with db.lock:
        conn = db.conn
        # Self-referral guard (cheap & explicit). Bad ref ids are silently
        # dropped — we can't validate that the referrer exists yet without an
        # extra roundtrip, and the FK is nullable so a NULL is the safe default.
        if referred_by == user_id:
            referred_by = None

        await conn.execute(
            """INSERT INTO users (user_id, username, full_name, referred_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username  = excluded.username,
                   full_name = excluded.full_name""",
            (user_id, username, full_name, referred_by),
        )
        check = await conn.execute(
            "SELECT 1 FROM subscriptions WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        has_sub = await check.fetchone()
        await conn.commit()
        return has_sub is None


async def mark_user_inactive(user_id: int) -> None:
    async with db.lock:
        await db.conn.execute(
            "UPDATE users SET is_active = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.conn.commit()


async def get_user(user_id: int) -> dict | None:
    cur = await db.conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    )
    user = await cur.fetchone()
    return dict(user) if user else None


async def get_all_active_users() -> list[dict]:
    cur = await db.conn.execute("SELECT * FROM users WHERE is_active = 1")
    return [dict(r) for r in await cur.fetchall()]


async def get_users_count() -> int:
    cur = await db.conn.execute("SELECT COUNT(*) AS cnt FROM users")
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


# ── Subscription operations ──

async def create_trial(user_id: int, bonus_days: int = 0) -> None:
    """Create a trial subscription. `bonus_days` extends the default trial
    length — used when a user lands via a referral link."""
    bonus = max(0, int(bonus_days))
    expires = _utcnow() + timedelta(days=config.trial_days + bonus)
    async with db.lock:
        await db.conn.execute(
            "INSERT INTO subscriptions (user_id, plan, tier, expires_at) "
            "VALUES (?, 'trial', ?, ?)",
            (user_id, config.trial_tier, expires.isoformat()),
        )
        await db.conn.commit()


async def get_active_subscription(user_id: int) -> dict | None:
    cur = await db.conn.execute(
        """SELECT * FROM subscriptions
           WHERE user_id = ? AND is_active = 1 AND expires_at > datetime('now')
           ORDER BY expires_at DESC LIMIT 1""",
        (user_id,),
    )
    sub = await cur.fetchone()
    return dict(sub) if sub else None


async def activate_subscription(
    user_id: int, tier: str, plan: str, payment_id: str
) -> None:
    """Activate a paid subscription on `tier` for `plan` duration, preserving
    any unused time from a previous active subscription (e.g. trial)."""
    days = 365 if plan == "yearly" else 30
    now = _utcnow()
    async with db.lock:
        cur = await db.conn.execute(
            """SELECT MAX(expires_at) AS expires
               FROM subscriptions
               WHERE user_id = ? AND is_active = 1 AND expires_at > datetime('now')""",
            (user_id,),
        )
        row = await cur.fetchone()
        existing_expires: datetime = now
        if row and row["expires"]:
            try:
                parsed = datetime.fromisoformat(row["expires"])
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed > existing_expires:
                    existing_expires = parsed
            except ValueError:
                pass

        # Spend any unconsumed bonus-days credits (referrer / promo).
        bonus_cur = await db.conn.execute(
            "SELECT COALESCE(SUM(days), 0) AS d FROM bonus_days_ledger "
            "WHERE user_id = ? AND consumed_at IS NULL",
            (user_id,),
        )
        bonus_row = await bonus_cur.fetchone()
        bonus_days = int(bonus_row["d"]) if bonus_row else 0

        new_expires = existing_expires + timedelta(days=days + bonus_days)

        await db.conn.execute(
            "UPDATE subscriptions SET is_active = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.conn.execute(
            "INSERT INTO subscriptions (user_id, plan, tier, expires_at, payment_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, plan, tier, new_expires.isoformat(), payment_id),
        )
        if bonus_days:
            await db.conn.execute(
                "UPDATE bonus_days_ledger SET consumed_at = datetime('now') "
                "WHERE user_id = ? AND consumed_at IS NULL",
                (user_id,),
            )
        await db.conn.commit()


async def get_subscribed_users() -> list[dict]:
    cur = await db.conn.execute(
        """SELECT u.user_id, u.username, u.full_name,
                  s.id AS subscription_id, s.tier, s.last_delivery_at, s.plan
           FROM users u
           JOIN subscriptions s ON u.user_id = s.user_id
           WHERE s.is_active = 1 AND s.expires_at > datetime('now') AND u.is_active = 1"""
    )
    return [dict(r) for r in await cur.fetchall()]


async def mark_subscription_delivered(subscription_id: int) -> None:
    """Stamp `last_delivery_at = now()` on a subscription so the per-tier
    delivery cooldown can be enforced on the next scheduler tick."""
    async with db.lock:
        await db.conn.execute(
            "UPDATE subscriptions SET last_delivery_at = datetime('now') WHERE id = ?",
            (subscription_id,),
        )
        await db.conn.commit()


# ── Reminders & upsell ──

async def get_subscriptions_needing_reminder(days_before: int) -> list[dict]:
    """Return active paid subscriptions that should receive a "your subscription
    expires in `days_before` days" reminder *now*.

    A reminder is due when:
      * the subscription is paid (plan != 'trial') and active;
      * `expires_at` falls inside (now + days_before - 1, now + days_before];
      * the corresponding `reminder_<days>d_sent_at` is NULL.

    The 24-hour window prevents missing/duplicating a reminder if the daily
    job runs slightly off-schedule.
    """
    if days_before == 3:
        sent_col = "reminder_3d_sent_at"
    elif days_before == 1:
        sent_col = "reminder_1d_sent_at"
    else:
        raise ValueError(f"Unsupported reminder window: {days_before}")

    upper_hours = days_before * 24
    lower_hours = upper_hours - 24

    cur = await db.conn.execute(
        f"""
        SELECT s.*, u.user_id AS uid
        FROM subscriptions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.is_active = 1
          AND s.plan != 'trial'
          AND u.is_active = 1
          AND {sent_col} IS NULL
          AND s.expires_at > datetime('now', ?)
          AND s.expires_at <= datetime('now', ?)
        """,
        (f"+{lower_hours} hours", f"+{upper_hours} hours"),
    )
    return [dict(r) for r in await cur.fetchall()]


async def mark_reminder_sent(subscription_id: int, days_before: int) -> None:
    if days_before == 3:
        col = "reminder_3d_sent_at"
    elif days_before == 1:
        col = "reminder_1d_sent_at"
    else:
        raise ValueError(f"Unsupported reminder window: {days_before}")
    async with db.lock:
        await db.conn.execute(
            f"UPDATE subscriptions SET {col} = datetime('now') WHERE id = ?",
            (subscription_id,),
        )
        await db.conn.commit()


async def can_send_upsell(
    subscription_id: int, throttle_seconds: int = 24 * 3600
) -> bool:
    """Return True if `throttle_seconds` have elapsed since the last upsell to
    this subscription (or none was sent yet)."""
    cur = await db.conn.execute(
        "SELECT last_upsell_at FROM subscriptions WHERE id = ?",
        (subscription_id,),
    )
    row = await cur.fetchone()
    if not row or not row["last_upsell_at"]:
        return True
    cur = await db.conn.execute(
        "SELECT (julianday('now') - julianday(?)) * 86400 AS sec",
        (row["last_upsell_at"],),
    )
    sec_row = await cur.fetchone()
    return bool(sec_row and sec_row["sec"] is not None and sec_row["sec"] >= throttle_seconds)


async def mark_upsell_sent(subscription_id: int) -> None:
    async with db.lock:
        await db.conn.execute(
            "UPDATE subscriptions SET last_upsell_at = datetime('now') WHERE id = ?",
            (subscription_id,),
        )
        await db.conn.commit()


async def count_remaining_unsent_for_user(user_id: int) -> int:
    """Cheap count of how many *more* matching orders are waiting for this user
    after a delivery batch — used to decide whether to nudge them to a higher
    tier. Returns 0 if their categories or platforms aren't set."""
    categories = await get_user_categories(user_id)
    platform_codes = await get_user_platforms(user_id)
    if not categories or not platform_codes:
        return 0
    platform_names = [PLATFORM_CODE_TO_NAME.get(c, c) for c in platform_codes]
    plat_placeholders = ",".join("?" for _ in platform_names)
    cat_conditions = " OR ".join("o.category LIKE ?" for _ in categories)
    query = f"""
        SELECT COUNT(*) AS cnt FROM orders o
        WHERE o.platform IN ({plat_placeholders})
          AND ({cat_conditions})
          AND o.id NOT IN (SELECT order_id FROM sent_orders WHERE user_id = ?)
    """
    params: list = []
    params.extend(platform_names)
    params.extend(f"%{c}%" for c in categories)
    params.append(user_id)
    cur = await db.conn.execute(query, params)
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


# ── Category operations ──

async def set_user_categories(user_id: int, categories: list[str]) -> None:
    async with db.lock:
        await db.conn.execute(
            "DELETE FROM user_categories WHERE user_id = ?", (user_id,)
        )
        for cat in categories:
            await db.conn.execute(
                "INSERT OR IGNORE INTO user_categories (user_id, category) VALUES (?, ?)",
                (user_id, cat),
            )
        await db.conn.commit()


async def toggle_user_category(user_id: int, category: str) -> bool:
    """Toggle a single category. Returns True if it is now selected."""
    async with db.lock:
        cur = await db.conn.execute(
            "SELECT 1 FROM user_categories WHERE user_id = ? AND category = ?",
            (user_id, category),
        )
        exists = await cur.fetchone()
        if exists:
            await db.conn.execute(
                "DELETE FROM user_categories WHERE user_id = ? AND category = ?",
                (user_id, category),
            )
            await db.conn.commit()
            return False
        await db.conn.execute(
            "INSERT OR IGNORE INTO user_categories (user_id, category) VALUES (?, ?)",
            (user_id, category),
        )
        await db.conn.commit()
        return True


async def get_user_categories(user_id: int) -> list[str]:
    cur = await db.conn.execute(
        "SELECT category FROM user_categories WHERE user_id = ?", (user_id,)
    )
    return [r["category"] for r in await cur.fetchall()]


# ── Platform operations ──

async def set_user_platforms(user_id: int, platforms: list[str]) -> None:
    async with db.lock:
        await db.conn.execute(
            "DELETE FROM user_platforms WHERE user_id = ?", (user_id,)
        )
        for p in platforms:
            await db.conn.execute(
                "INSERT OR IGNORE INTO user_platforms (user_id, platform) VALUES (?, ?)",
                (user_id, p),
            )
        await db.conn.commit()


async def toggle_user_platform(user_id: int, platform: str) -> bool:
    """Toggle a single platform. Returns True if it is now selected."""
    async with db.lock:
        cur = await db.conn.execute(
            "SELECT 1 FROM user_platforms WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        )
        exists = await cur.fetchone()
        if exists:
            await db.conn.execute(
                "DELETE FROM user_platforms WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            )
            await db.conn.commit()
            return False
        await db.conn.execute(
            "INSERT OR IGNORE INTO user_platforms (user_id, platform) VALUES (?, ?)",
            (user_id, platform),
        )
        await db.conn.commit()
        return True


async def get_user_platforms(user_id: int) -> list[str]:
    cur = await db.conn.execute(
        "SELECT platform FROM user_platforms WHERE user_id = ?", (user_id,)
    )
    return [r["platform"] for r in await cur.fetchall()]


# ── Order operations ──

# In-memory LRU cache of recently-seen (platform, external_id) tuples.
# Parsers re-fetch the same RSS/HTML feed every tick so 90%+ of orders are
# already in the DB. Hitting this cache avoids the SQLite roundtrip entirely.
# `_order_seen` keeps insertion order; we cap it at `_ORDER_CACHE_SIZE` and
# evict the oldest entries (poor man's LRU; fine for our access pattern).
_ORDER_CACHE_SIZE = 10_000
_order_seen: "OrderedDict[tuple[str, str], None]" = OrderedDict()


def _cache_seen(platform: str, external_id: str) -> None:
    key = (platform, external_id)
    if key in _order_seen:
        _order_seen.move_to_end(key)
        return
    _order_seen[key] = None
    if len(_order_seen) > _ORDER_CACHE_SIZE:
        _order_seen.popitem(last=False)


def _is_seen(platform: str, external_id: str) -> bool:
    return (platform, external_id) in _order_seen


async def save_orders(orders: list[dict]) -> list[int]:
    if not orders:
        return []
    new_ids: list[int] = []
    async with db.lock:
        for order in orders:
            platform = order["platform"]
            ext_id = order["external_id"]
            # Cheap in-memory dedup before paying the SQLite roundtrip cost.
            if _is_seen(platform, ext_id):
                continue
            try:
                cur = await db.conn.execute(
                    """INSERT OR IGNORE INTO orders
                       (platform, external_id, title, description, category, price, url)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        platform,
                        ext_id,
                        order["title"],
                        order.get("description", ""),
                        order.get("category", ""),
                        order.get("price", ""),
                        order["url"],
                    ),
                )
                if cur.lastrowid and cur.rowcount > 0:
                    new_ids.append(cur.lastrowid)
                # Either we just inserted it, or it was already in the DB —
                # either way it's "seen" now.
                _cache_seen(platform, ext_id)
            except Exception:
                logger.exception("Failed to insert order %s", ext_id)
                continue
        await db.conn.commit()
    return new_ids


PLATFORM_CODE_TO_NAME = {
    "kwork": "Kwork",
    "fl_ru": "FL.ru",
    "freelance_ru": "Freelance.ru",
    "weblancer": "Weblancer",
    "youdo": "YouDo",
}


async def get_unsent_orders_for_user(user_id: int, limit: int = 20) -> list[dict]:
    """Return orders that haven't been sent to `user_id` yet, filtered by their
    selected categories AND platforms.

    Both filters are required: a user with categories but no platforms (or
    vice versa) gets nothing — otherwise we'd flood them with orders from
    sources they didn't opt into.
    """
    categories = await get_user_categories(user_id)
    platform_codes = await get_user_platforms(user_id)

    if not categories or not platform_codes:
        return []

    platform_names = [PLATFORM_CODE_TO_NAME.get(c, c) for c in platform_codes]
    plat_placeholders = ",".join("?" for _ in platform_names)
    cat_conditions = " OR ".join("o.category LIKE ?" for _ in categories)

    query = f"""
        SELECT o.* FROM orders o
        WHERE o.platform IN ({plat_placeholders})
          AND ({cat_conditions})
          AND o.id NOT IN (SELECT order_id FROM sent_orders WHERE user_id = ?)
        ORDER BY o.created_at DESC
        LIMIT ?
    """

    params: list = []
    params.extend(platform_names)
    params.extend(f"%{c}%" for c in categories)
    params.append(user_id)
    params.append(int(limit))

    cur = await db.conn.execute(query, params)
    return [dict(r) for r in await cur.fetchall()]


async def mark_order_sent(user_id: int, order_id: int) -> None:
    async with db.lock:
        await db.conn.execute(
            "INSERT OR IGNORE INTO sent_orders (user_id, order_id) VALUES (?, ?)",
            (user_id, order_id),
        )
        await db.conn.commit()


async def is_order_sent(user_id: int, order_id: int) -> bool:
    cur = await db.conn.execute(
        "SELECT 1 FROM sent_orders WHERE user_id = ? AND order_id = ?",
        (user_id, order_id),
    )
    return (await cur.fetchone()) is not None


async def get_orders_count() -> int:
    cur = await db.conn.execute("SELECT COUNT(*) AS cnt FROM orders")
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


# ── Payment operations ──

async def save_payment(
    user_id: int,
    payment_id: str,
    amount: float,
    tier: str,
    plan: str,
    provider: str = "yookassa",
    currency: str = "RUB",
) -> None:
    async with db.lock:
        await db.conn.execute(
            "INSERT INTO payments (user_id, payment_id, amount, tier, plan, "
            "provider, currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, payment_id, amount, tier, plan, provider, currency),
        )
        await db.conn.commit()


async def update_payment_status(payment_id: str, status: str) -> None:
    async with db.lock:
        await db.conn.execute(
            "UPDATE payments SET status = ? WHERE payment_id = ?",
            (status, payment_id),
        )
        await db.conn.commit()


async def get_payment(payment_id: str) -> dict | None:
    cur = await db.conn.execute(
        "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
    )
    payment = await cur.fetchone()
    return dict(payment) if payment else None


# ── Referrals ──

import json as _json
import secrets as _secrets

# Alphabet for the random referral code: uppercase letters + digits, minus
# look-alikes (0/O, 1/I/L). Keeps the code easy to read out loud / type by hand.
_REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _generate_ref_code(length: int) -> str:
    return "".join(_secrets.choice(_REF_ALPHABET) for _ in range(length))


async def get_or_create_referral_code(user_id: int, length: int) -> str:
    """Return the user's referral code, generating it lazily if needed.

    We retry up to 8 times if a freshly generated code collides — at length 7
    the alphabet is large enough that this is essentially never reached.
    """
    cur = await db.conn.execute(
        "SELECT referral_code FROM users WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    if row and row["referral_code"]:
        return row["referral_code"]

    async with db.lock:
        for _ in range(8):
            code = _generate_ref_code(length)
            try:
                await db.conn.execute(
                    "UPDATE users SET referral_code = ? "
                    "WHERE user_id = ? AND referral_code IS NULL",
                    (code, user_id),
                )
                cur2 = await db.conn.execute(
                    "SELECT referral_code FROM users WHERE user_id = ?",
                    (user_id,),
                )
                row2 = await cur2.fetchone()
                await db.conn.commit()
                if row2 and row2["referral_code"]:
                    return row2["referral_code"]
            except Exception:
                logger.exception("Failed to assign ref code (will retry)")
                continue
    raise RuntimeError(f"Could not assign a referral code to user {user_id}")


async def find_user_by_referral_code(code: str) -> dict | None:
    """Resolve a referral code to its owner. Codes are stored as-typed; we
    upper-case the input so users don't trip on capitalisation."""
    if not code:
        return None
    cur = await db.conn.execute(
        "SELECT * FROM users WHERE referral_code = ?", (code.upper(),)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def count_referrals(user_id: int) -> int:
    """How many people signed up using this user's referral code."""
    cur = await db.conn.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE referred_by = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


async def get_referrer_id(user_id: int) -> int | None:
    cur = await db.conn.execute(
        "SELECT referred_by FROM users WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    return int(row["referred_by"]) if row and row["referred_by"] else None


# ── Bonus-days ledger ──

async def add_bonus_days(
    user_id: int, days: int, source: str, source_ref: str | None = None
) -> None:
    """Append a credit to the bonus-days ledger. The credit is consumed at
    the next call to `activate_subscription`."""
    if days == 0:
        return
    async with db.lock:
        await db.conn.execute(
            "INSERT INTO bonus_days_ledger (user_id, days, source, source_ref) "
            "VALUES (?, ?, ?, ?)",
            (user_id, days, source, source_ref),
        )
        await db.conn.commit()


async def get_unconsumed_bonus_days(user_id: int) -> int:
    cur = await db.conn.execute(
        "SELECT COALESCE(SUM(days), 0) AS d FROM bonus_days_ledger "
        "WHERE user_id = ? AND consumed_at IS NULL",
        (user_id,),
    )
    row = await cur.fetchone()
    return int(row["d"]) if row else 0


# ── Promo codes ──

async def get_promo_code(code: str) -> dict | None:
    """Return a promo code by its label, or None if missing or expired.

    Note: this is the caller-facing reader used by /promo. It hides expired
    codes so the user sees the same generic failure message regardless of root
    cause. `redeem_promo_code` re-checks expiry under lock, so even if a code
    expires between `get_promo_code` and `redeem_promo_code` we still refuse
    the redemption.
    """
    if not code:
        return None
    cur = await db.conn.execute(
        "SELECT * FROM promo_codes "
        "WHERE code = ? "
        "  AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (code.upper(),),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_pending_discount_for_user(user_id: int) -> dict | None:
    """Return the most recent unconsumed `discount_pct` redemption for `user_id`.

    "Unconsumed" means `promo_redemptions.payment_id IS NULL` — i.e. the user
    activated the code but no paid checkout has linked to it yet. We also
    filter out redemptions whose code has since expired so an old reservation
    can't be honoured forever.

    Returns a dict with keys `redemption_id`, `code_id`, `code`, `pct` (int).
    """
    cur = await db.conn.execute(
        "SELECT pr.id AS redemption_id, pr.code_id, pc.code, "
        "       pc.value AS pct "
        "FROM promo_redemptions pr "
        "JOIN promo_codes pc ON pc.id = pr.code_id "
        "WHERE pr.user_id = ? "
        "  AND pr.payment_id IS NULL "
        "  AND pc.kind = 'discount_pct' "
        "  AND (pc.expires_at IS NULL OR pc.expires_at > datetime('now')) "
        "ORDER BY pr.id DESC LIMIT 1",
        (user_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def mark_discount_redemption_consumed(
    redemption_id: int, payment_id: str
) -> None:
    """Stamp `payment_id` onto a redemption row so the next call to
    `get_pending_discount_for_user` no longer returns it. Idempotent: a second
    call with the same id is a no-op."""
    async with db.lock:
        await db.conn.execute(
            "UPDATE promo_redemptions SET payment_id = ? "
            "WHERE id = ? AND payment_id IS NULL",
            (payment_id, redemption_id),
        )
        await db.conn.commit()


async def has_user_redeemed_promo(code_id: int, user_id: int) -> bool:
    cur = await db.conn.execute(
        "SELECT 1 FROM promo_redemptions WHERE code_id = ? AND user_id = ?",
        (code_id, user_id),
    )
    return (await cur.fetchone()) is not None


async def redeem_promo_code(
    code_id: int, user_id: int, payment_id: str | None = None
) -> bool:
    """Atomically register a promo redemption for `(code, user)`. Returns
    False if the code was already redeemed by the user, exhausted, or
    expired in the meantime — caller should treat False as "do nothing".
    """
    async with db.lock:
        # Re-read the code under lock so concurrent redemptions can't both
        # pass the max_uses check.
        cur = await db.conn.execute(
            "SELECT * FROM promo_codes WHERE id = ?", (code_id,)
        )
        promo = await cur.fetchone()
        if not promo:
            return False
        if promo["expires_at"]:
            cur2 = await db.conn.execute(
                "SELECT datetime(?) <= datetime('now') AS expired",
                (promo["expires_at"],),
            )
            row2 = await cur2.fetchone()
            if row2 and row2["expired"]:
                return False
        if promo["max_uses"] is not None and promo["used_count"] >= promo["max_uses"]:
            return False
        try:
            await db.conn.execute(
                "INSERT INTO promo_redemptions (code_id, user_id, payment_id) "
                "VALUES (?, ?, ?)",
                (code_id, user_id, payment_id),
            )
        except Exception:
            # UNIQUE(code_id, user_id) already taken — treat as already redeemed.
            await db.conn.commit()
            return False
        await db.conn.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE id = ?",
            (code_id,),
        )
        await db.conn.commit()
        return True


async def create_promo_code(
    code: str,
    kind: str,
    value: int,
    *,
    max_uses: int | None = None,
    expires_at: str | None = None,
    created_by: int | None = None,
    note: str | None = None,
) -> int:
    """Insert a new promo code. Returns the new code's id, or raises if the
    code already exists."""
    async with db.lock:
        cur = await db.conn.execute(
            "INSERT INTO promo_codes "
            "(code, kind, value, max_uses, expires_at, created_by, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code.upper(), kind, value, max_uses, expires_at, created_by, note),
        )
        await db.conn.commit()
        return int(cur.lastrowid or 0)


# ── Events ──

async def log_event(
    event_type: str,
    user_id: int | None = None,
    properties: dict | None = None,
) -> None:
    """Append an analytics event. Best-effort: never raises into the caller.

    `properties` is JSON-serialised; values that aren't JSON-serialisable are
    coerced to strings so a stray Decimal or Path can't blow up the call site.
    """
    try:
        payload: str | None = None
        if properties:
            try:
                payload = _json.dumps(properties, ensure_ascii=False)
            except (TypeError, ValueError):
                payload = _json.dumps(
                    {k: str(v) for k, v in properties.items()},
                    ensure_ascii=False,
                )
        async with db.lock:
            await db.conn.execute(
                "INSERT INTO events (user_id, event_type, properties) "
                "VALUES (?, ?, ?)",
                (user_id, event_type, payload),
            )
            await db.conn.commit()
    except Exception:
        logger.exception("Failed to log event %s", event_type)


async def count_events(event_type: str, since_iso: str | None = None) -> int:
    """Cheap count for admin dashboards. `since_iso` is a SQLite-compatible
    timestamp like '2026-01-01' or '2026-01-01 00:00:00'."""
    if since_iso:
        cur = await db.conn.execute(
            "SELECT COUNT(*) AS c FROM events "
            "WHERE event_type = ? AND created_at >= ?",
            (event_type, since_iso),
        )
    else:
        cur = await db.conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = ?",
            (event_type,),
        )
    row = await cur.fetchone()
    return int(row["c"]) if row else 0


async def fetch_events(limit: int = 1000, since_iso: str | None = None) -> list[dict]:
    """Pull a slice of events for CSV export. Newest first."""
    params: list = []
    where = ""
    if since_iso:
        where = "WHERE created_at >= ?"
        params.append(since_iso)
    params.append(limit)
    cur = await db.conn.execute(
        f"SELECT id, user_id, event_type, properties, created_at FROM events "
        f"{where} ORDER BY id DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cur.fetchall()]


async def fetch_payments(limit: int = 1000) -> list[dict]:
    cur = await db.conn.execute(
        "SELECT id, user_id, payment_id, amount, currency, status, plan, "
        "tier, provider, created_at FROM payments ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ── Admin analytics helpers ──

async def stats_users() -> dict:
    """Counts users we use in /admin stats. One query per metric for clarity."""
    out: dict = {}
    cur = await db.conn.execute("SELECT COUNT(*) AS c FROM users")
    out["total"] = int((await cur.fetchone())["c"])
    cur = await db.conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_active = 1"
    )
    out["active"] = int((await cur.fetchone())["c"])
    cur = await db.conn.execute(
        "SELECT COUNT(*) AS c FROM users "
        "WHERE registered_at >= datetime('now', '-1 day')"
    )
    out["new_24h"] = int((await cur.fetchone())["c"])
    cur = await db.conn.execute(
        "SELECT COUNT(*) AS c FROM users "
        "WHERE registered_at >= datetime('now', '-7 days')"
    )
    out["new_7d"] = int((await cur.fetchone())["c"])
    return out


async def stats_subscriptions() -> dict:
    """Active subs by tier + plan, plus trial vs paid breakdown."""
    out: dict = {"by_tier": {}, "trial": 0, "paid": 0}
    cur = await db.conn.execute(
        "SELECT tier, COUNT(*) AS c FROM subscriptions "
        "WHERE is_active = 1 AND expires_at > datetime('now') "
        "GROUP BY tier"
    )
    for r in await cur.fetchall():
        out["by_tier"][r["tier"]] = int(r["c"])
    cur = await db.conn.execute(
        "SELECT "
        "SUM(CASE WHEN plan = 'trial' THEN 1 ELSE 0 END) AS trial, "
        "SUM(CASE WHEN plan != 'trial' THEN 1 ELSE 0 END) AS paid "
        "FROM subscriptions "
        "WHERE is_active = 1 AND expires_at > datetime('now')"
    )
    row = await cur.fetchone()
    if row:
        out["trial"] = int(row["trial"] or 0)
        out["paid"] = int(row["paid"] or 0)
    return out


async def stats_revenue(currency: str = "RUB") -> dict:
    """Sum of paid revenue + ARPU per tier in the requested currency."""
    out: dict = {"total": 0.0, "by_tier": {}, "currency": currency}
    cur = await db.conn.execute(
        "SELECT tier, COALESCE(SUM(amount), 0) AS rev, COUNT(*) AS cnt "
        "FROM payments "
        "WHERE status = 'succeeded' AND currency = ? "
        "GROUP BY tier",
        (currency,),
    )
    for r in await cur.fetchall():
        rev = float(r["rev"] or 0)
        cnt = int(r["cnt"] or 0)
        out["by_tier"][r["tier"]] = {
            "revenue": rev,
            "count": cnt,
            "arpu": (rev / cnt) if cnt else 0.0,
        }
        out["total"] += rev
    return out


async def stats_conversion() -> dict:
    """Trial → paid conversion: users who started a trial AND later activated
    a paid subscription, divided by users who started a trial.

    A "started trial" user is one with a row in `subscriptions` with
    `plan = 'trial'`. A "converted" user is one of those who *also* has a
    paid (plan != 'trial') subscription row.
    """
    cur = await db.conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS c FROM subscriptions "
        "WHERE plan = 'trial'"
    )
    trial_users = int((await cur.fetchone())["c"])
    cur = await db.conn.execute(
        "SELECT COUNT(DISTINCT t.user_id) AS c FROM subscriptions t "
        "JOIN subscriptions p ON p.user_id = t.user_id AND p.plan != 'trial' "
        "WHERE t.plan = 'trial'"
    )
    converted = int((await cur.fetchone())["c"])
    return {
        "trial_users": trial_users,
        "converted": converted,
        "rate_pct": (round(100 * converted / trial_users, 1) if trial_users else 0.0),
    }


async def stats_churn() -> dict:
    """Approximate churn: paid users whose latest non-trial subscription
    has expired and who do NOT currently have an active paid one.
    """
    cur = await db.conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS c FROM subscriptions "
        "WHERE plan != 'trial'"
    )
    ever_paid = int((await cur.fetchone())["c"])
    cur = await db.conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS c FROM subscriptions "
        "WHERE plan != 'trial' AND is_active = 1 "
        "AND expires_at > datetime('now')"
    )
    still_paid = int((await cur.fetchone())["c"])
    churned = ever_paid - still_paid
    return {
        "ever_paid": ever_paid,
        "still_paid": still_paid,
        "churned": churned,
        "rate_pct": (round(100 * churned / ever_paid, 1) if ever_paid else 0.0),
    }
