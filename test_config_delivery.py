"""Tests for config.py delivery speed env overrides.

Verifies that ORDERS_PER_TICK_* and DELIVERY_COOLDOWN_* env vars
are correctly loaded, validated, and exposed via config helpers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("YOOKASSA_SHOP_ID", "test")
os.environ.setdefault("YOOKASSA_SECRET_KEY", "test")

import pytest
from bot.constants import TIER_BASIC, TIER_PRO, TIER_MAX


def _make_config(**env_overrides):
    """Create a fresh Config with the given env vars temporarily set."""
    import importlib
    old = {k: os.environ.get(k) for k in env_overrides}
    for k, v in env_overrides.items():
        os.environ[k] = str(v)
    try:
        import bot.config as cfg_mod
        importlib.reload(cfg_mod)
        # Re-instantiate so __post_init__ picks up the new env.
        return cfg_mod.Config()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestDefaultDeliverySpeed:
    """Default values from constants are used when env vars are absent."""

    def test_default_orders_per_tick_basic(self):
        cfg = _make_config()
        assert cfg.orders_per_tick(TIER_BASIC) == 10

    def test_default_orders_per_tick_pro(self):
        cfg = _make_config()
        assert cfg.orders_per_tick(TIER_PRO) == 30

    def test_default_orders_per_tick_max(self):
        cfg = _make_config()
        assert cfg.orders_per_tick(TIER_MAX) == 100

    def test_default_cooldown_basic_is_3min(self):
        cfg = _make_config()
        assert cfg.delivery_cooldown(TIER_BASIC) == 180

    def test_default_cooldown_pro_is_1min(self):
        cfg = _make_config()
        assert cfg.delivery_cooldown(TIER_PRO) == 60

    def test_default_cooldown_max_is_30sec(self):
        cfg = _make_config()
        assert cfg.delivery_cooldown(TIER_MAX) == 30


class TestEnvOverride:
    """Values from .env override the defaults."""

    def test_orders_per_tick_overridden_via_env(self):
        cfg = _make_config(ORDERS_PER_TICK_PRO="50")
        assert cfg.orders_per_tick(TIER_PRO) == 50
        # Other tiers unchanged.
        assert cfg.orders_per_tick(TIER_BASIC) == 10

    def test_delivery_cooldown_overridden_via_env(self):
        cfg = _make_config(DELIVERY_COOLDOWN_MAX="15")
        assert cfg.delivery_cooldown(TIER_MAX) == 15

    def test_all_three_tiers_overridden_independently(self):
        cfg = _make_config(
            ORDERS_PER_TICK_BASIC="5",
            ORDERS_PER_TICK_PRO="20",
            ORDERS_PER_TICK_MAX="200",
        )
        assert cfg.orders_per_tick(TIER_BASIC) == 5
        assert cfg.orders_per_tick(TIER_PRO) == 20
        assert cfg.orders_per_tick(TIER_MAX) == 200

    def test_cooldown_all_tiers_overridden(self):
        cfg = _make_config(
            DELIVERY_COOLDOWN_BASIC="120",
            DELIVERY_COOLDOWN_PRO="45",
            DELIVERY_COOLDOWN_MAX="10",
        )
        assert cfg.delivery_cooldown(TIER_BASIC) == 120
        assert cfg.delivery_cooldown(TIER_PRO) == 45
        assert cfg.delivery_cooldown(TIER_MAX) == 10


class TestCooldownLabel:
    """cooldown_label() returns human-readable strings."""

    def test_seconds_below_60_shows_sec(self):
        cfg = _make_config(DELIVERY_COOLDOWN_MAX="30")
        assert cfg.cooldown_label(TIER_MAX) == "30 сек"

    def test_exactly_60_shows_1_min(self):
        cfg = _make_config(DELIVERY_COOLDOWN_PRO="60")
        assert cfg.cooldown_label(TIER_PRO) == "1 мин"

    def test_180_shows_3_min(self):
        cfg = _make_config(DELIVERY_COOLDOWN_BASIC="180")
        assert cfg.cooldown_label(TIER_BASIC) == "3 мин"

    def test_45_seconds_shows_sec(self):
        cfg = _make_config(DELIVERY_COOLDOWN_PRO="45")
        assert cfg.cooldown_label(TIER_PRO) == "45 сек"


class TestUnknownTierFallback:
    """Unknown tier gracefully falls back to Basic."""

    def test_orders_per_tick_unknown_tier_fallback(self):
        cfg = _make_config()
        # Unknown tier → falls back to Basic value, no crash.
        assert cfg.orders_per_tick("ultra") == cfg.orders_per_tick(TIER_BASIC)

    def test_delivery_cooldown_unknown_tier_fallback(self):
        cfg = _make_config()
        assert cfg.delivery_cooldown("vip") == cfg.delivery_cooldown(TIER_BASIC)


class TestValidation:
    """Config.validate() catches bad delivery speed values."""

    def test_zero_orders_per_tick_raises(self):
        cfg = _make_config(ORDERS_PER_TICK_PRO="0")
        with pytest.raises(Exception, match="ORDERS_PER_TICK_PRO"):
            cfg.validate(require_payments=False)

    def test_negative_cooldown_raises(self):
        cfg = _make_config(DELIVERY_COOLDOWN_MAX="-1")
        with pytest.raises(Exception, match="DELIVERY_COOLDOWN_MAX"):
            cfg.validate(require_payments=False)

    def test_valid_config_passes(self):
        cfg = _make_config(
            ORDERS_PER_TICK_BASIC="10",
            DELIVERY_COOLDOWN_BASIC="180",
        )
        # Should not raise.
        cfg.validate(require_payments=False)
