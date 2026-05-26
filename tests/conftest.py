"""Shared test setup. Adds the project root to `sys.path` so tests can import
`bot.*` without an installed package, and provides fixtures common to several
test modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Provide harmless defaults so `bot.config` can import without a real .env.
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("YOOKASSA_SHOP_ID", "test")
os.environ.setdefault("YOOKASSA_SECRET_KEY", "test")
