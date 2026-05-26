"""Tests for the tiered subscription system.

Covers:
* per-tier price lookup
* `pay:<tier>:<plan>` callback parser (rejects malformed / unknown values)
* schema migration that adds `tier` and `last_delivery_at` columns
* per-tier delivery cooldown helper
* tier-aware `format_subscription_info`
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from bot.config import Config, ConfigError, config
from bot.constants import (
    TIER_BADGE_LABEL,
    TIER_BASIC,
    TIER_CUSTOM_EMOJI_ID,
    TIER_DELIVERY_COOLDOWN_SECONDS,
    TIER_LABEL,
    TIER_MAX,
    TIER_ORDERS_PER_TICK,
    TIER_PRO,
    TIERS,
)
from bot.handlers.payment import _parse_pay_callback
from bot.handlers.subscription import (
    _comparison_vs_basic,
    _orders_per_hour,
    _tier_badge,
    _tier_icon,
    _tiers_pricing_block,
    format_subscription_info,
)
from bot.keyboards import subscription_kb
from bot.scheduler import _seconds_since


class TestPriceLookup:
    def test_default_prices_are_strictly_increasing(self):
        # Higher tiers should cost more — nothing else makes sense.
        assert (
            config.price_for(TIER_BASIC, "monthly")
            < config.price_for(TIER_PRO, "monthly")
            < config.price_for(TIER_MAX, "monthly")
        )
        assert (
            config.price_for(TIER_BASIC, "yearly")
            < config.price_for(TIER_PRO, "yearly")
            < config.price_for(TIER_MAX, "yearly")
        )

    def test_yearly_is_cheaper_than_12_monthly(self):
        for tier in TIERS:
            assert (
                config.price_for(tier, "yearly")
                < 12 * config.price_for(tier, "monthly")
            ), f"yearly should give a discount over 12*monthly for {tier}"

    def test_unknown_tier_raises(self):
        with pytest.raises(ConfigError):
            config.price_for("ultra", "monthly")

    def test_unknown_plan_raises(self):
        with pytest.raises(ConfigError):
            config.price_for(TIER_BASIC, "lifetime")


class TestPayCallback:
    @pytest.mark.parametrize("tier", list(TIERS))
    @pytest.mark.parametrize("plan", ["monthly", "yearly"])
    def test_valid(self, tier, plan):
        assert _parse_pay_callback(f"pay:{tier}:{plan}") == (tier, plan)

    def test_legacy_two_segment_form_rejected(self):
        # Old `pay:monthly` callbacks must NOT be accepted: otherwise an
        # attacker could replay an old button to skip tier validation.
        assert _parse_pay_callback("pay:monthly") is None
        assert _parse_pay_callback("pay:yearly") is None

    @pytest.mark.parametrize("data", [
        "pay:basic:lifetime",
        "pay:ultra:monthly",
        "pay::monthly",
        "pay:basic:",
        "pay:basic:monthly:extra",
        "",
        "buy:basic:monthly",
    ])
    def test_invalid(self, data):
        assert _parse_pay_callback(data) is None


class TestSecondsSince:
    def test_none_returns_inf(self):
        assert _seconds_since(None) == float("inf")
        assert _seconds_since("") == float("inf")

    def test_invalid_returns_inf(self):
        assert _seconds_since("not a date") == float("inf")

    def test_recent_timestamp_returns_small_value(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert 5 < _seconds_since(ts) < 30

    def test_iso_format_with_tz(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        assert 100 < _seconds_since(ts) < 150


class TestValueProps:
    def test_yearly_savings_pct_positive(self):
        # The default prices give a meaningful discount for paying yearly.
        for tier in TIERS:
            assert config.yearly_savings_pct(tier) > 0, (
                f"yearly should be cheaper than 12*monthly for {tier}"
            )

    def test_pro_has_promotional_badge(self):
        # Without a "popular" badge on Pro the value-prop UX falls flat —
        # the whole point is to nudge users toward Pro.
        assert TIER_BADGE_LABEL[TIER_PRO]
        assert TIER_BADGE_LABEL[TIER_BASIC] == ""

    def test_comparison_vs_basic_for_pro(self):
        text = _comparison_vs_basic(TIER_PRO)
        assert "Basic" in text
        # Pro must claim more orders AND faster — both are key selling points.
        assert "заказов" in text
        assert "быстрее" in text

    def test_comparison_for_basic_is_empty(self):
        assert _comparison_vs_basic(TIER_BASIC) == ""

    def test_max_comparison_says_unlimited(self):
        text = _comparison_vs_basic(TIER_MAX)
        assert "без лимита" in text

    def test_orders_per_hour_pro_beats_basic(self):
        assert _orders_per_hour(TIER_PRO) > _orders_per_hour(TIER_BASIC)

    def test_tiers_pricing_block_shows_savings(self):
        block = _tiers_pricing_block()
        # Yearly savings as `−XX%` for at least Pro tier (the badge target).
        assert "−" in block and "%" in block

    def test_subscription_kb_buttons_show_value_props(self):
        kb = subscription_kb(has_active=False)
        flat = [b.text for row in kb.inline_keyboard for b in row]

        # Pro monthly button must have the marketing badge label.
        pro_monthly = next(t for t in flat if "Про" in t and "мес" in t)
        assert TIER_BADGE_LABEL[TIER_PRO] in pro_monthly

        # Yearly buttons must show a savings %.
        for plan_label in ("Обычный", "Про", "Макс"):
            yr_btn = next(t for t in flat if plan_label in t and "год" in t)
            assert "%" in yr_btn, f"yearly button for {plan_label} missing savings: {yr_btn!r}"

    def test_subscription_kb_buttons_have_no_premium_emoji_tags(self):
        # Custom <tg-emoji> tags don't render in inline buttons — make sure we
        # never accidentally leak them into button text.
        kb = subscription_kb(has_active=False)
        for row in kb.inline_keyboard:
            for btn in row:
                assert "<tg-emoji" not in btn.text, (
                    f"button leaked premium emoji tag: {btn.text!r}"
                )

    def test_tier_icon_uses_premium_emoji_when_id_set(self):
        # All default tier IDs are configured, so output must wrap in
        # <tg-emoji> and contain the tier's Unicode fallback.
        for tier in TIERS:
            rendered = _tier_icon(tier)
            assert TIER_CUSTOM_EMOJI_ID[tier] in rendered
            assert "<tg-emoji" in rendered
            assert "</tg-emoji>" in rendered

    def test_tier_badge_uses_premium_emoji_for_pro_max(self):
        # Pro and Max each get an icon-prefixed tag; Basic stays empty.
        assert _tier_badge(TIER_BASIC) == ""
        for tier in (TIER_PRO, TIER_MAX):
            rendered = _tier_badge(tier)
            assert "<tg-emoji" in rendered
            assert TIER_BADGE_LABEL[tier] in rendered

    def test_tiers_pricing_block_contains_premium_emoji_tags(self):
        # The whole tariffs card should be using premium icons by default —
        # otherwise the user's investment in custom emoji is wasted.
        block = _tiers_pricing_block()
        assert block.count("<tg-emoji") >= 5

    def test_format_subscription_info_no_sub_uses_premium(self):
        text = format_subscription_info(None)
        assert "<tg-emoji" in text

    def test_format_subscription_info_active_uses_premium(self):
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        sub = {"plan": "monthly", "tier": TIER_PRO, "expires_at": future}
        text = format_subscription_info(sub)
        assert "<tg-emoji" in text


class TestSubscriptionInfo:
    def test_no_subscription_lists_all_tiers(self):
        text = format_subscription_info(None)
        assert "нет активной подписки" in text
        for tier in TIERS:
            assert TIER_LABEL[tier] in text

    def test_active_subscription_shows_tier_and_cooldown(self):
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        sub = {
            "plan": "monthly",
            "tier": TIER_PRO,
            "expires_at": future,
        }
        text = format_subscription_info(sub)
        assert TIER_LABEL[TIER_PRO] in text
        assert "Месячный" in text
        cooldown_min = TIER_DELIVERY_COOLDOWN_SECONDS[TIER_PRO] // 60
        assert f"{cooldown_min} мин" in text
        per_tick = TIER_ORDERS_PER_TICK[TIER_PRO]
        assert f"до {per_tick} заказов" in text

    def test_max_tier_shows_unlimited(self):
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        sub = {"plan": "yearly", "tier": TIER_MAX, "expires_at": future}
        text = format_subscription_info(sub)
        assert "без лимита" in text


class TestSchemaMigration:
    def test_v1_db_is_migrated_to_latest(self):
        """Open a DB at schema v1 and confirm migration adds the new columns
        and stamps the legacy tier on existing rows."""
        async def _run():
            from bot import database as db_mod

            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "old.db")

                # Build a v1 database by hand (bypass migrate).
                import aiosqlite
                async with aiosqlite.connect(path) as conn:
                    await conn.executescript(
                        """
                        CREATE TABLE users (
                            user_id INTEGER PRIMARY KEY, username TEXT,
                            full_name TEXT, registered_at TEXT,
                            is_active INTEGER NOT NULL DEFAULT 1
                        );
                        CREATE TABLE subscriptions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL, plan TEXT NOT NULL,
                            started_at TEXT, expires_at TEXT NOT NULL,
                            is_active INTEGER NOT NULL DEFAULT 1,
                            payment_id TEXT
                        );
                        CREATE TABLE payments (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL,
                            payment_id TEXT NOT NULL UNIQUE,
                            amount REAL NOT NULL,
                            currency TEXT NOT NULL DEFAULT 'RUB',
                            status TEXT NOT NULL DEFAULT 'pending',
                            plan TEXT NOT NULL,
                            created_at TEXT
                        );
                        INSERT INTO users (user_id, username, full_name, registered_at)
                            VALUES (1, 'a', 'A', datetime('now'));
                        INSERT INTO subscriptions (user_id, plan, started_at, expires_at, is_active)
                            VALUES (1, 'monthly', datetime('now'), datetime('now', '+30 days'), 1);
                        """
                    )
                    await conn.execute("PRAGMA user_version = 1")
                    await conn.commit()

                # Re-point the singleton at our temp DB and run migration.
                old_db = db_mod.db
                db_mod.db = db_mod.Database(path)
                try:
                    await db_mod.init_db()
                    cur = await db_mod.db.conn.execute(
                        "SELECT tier, last_delivery_at, "
                        "reminder_3d_sent_at, reminder_1d_sent_at, last_upsell_at "
                        "FROM subscriptions WHERE user_id = 1"
                    )
                    row = await cur.fetchone()
                    assert row["tier"] == TIER_BASIC, (
                        "legacy subscriptions must default to basic"
                    )
                    assert row["last_delivery_at"] is None
                    # v3 reminder/upsell columns must default to NULL too.
                    assert row["reminder_3d_sent_at"] is None
                    assert row["reminder_1d_sent_at"] is None
                    assert row["last_upsell_at"] is None

                    cur = await db_mod.db.conn.execute("PRAGMA user_version")
                    version = (await cur.fetchone())[0]
                    assert version == db_mod.SCHEMA_VERSION
                finally:
                    await db_mod.close_db()
                    db_mod.db = old_db

        asyncio.run(_run())
