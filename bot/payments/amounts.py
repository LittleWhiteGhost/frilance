"""Shared payment-amount validation helpers.

Both the YooKassa webhook and the in-bot "I paid" callback need to verify that
the amount reported by YooKassa matches what we quoted the user. Keeping a
single comparator avoids the two call sites ever drifting apart.

The comparator works in `Decimal` so YooKassa's "599.00" string and our int
599 compare equal regardless of formatting.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def amount_matches(actual: object, expected: object) -> bool:
    """Return True iff `actual` parses to the same Decimal as `expected`.

    Both arguments can be int/float/str/Decimal. Anything that can't be coerced
    into a Decimal (None, garbage strings, weird objects) compares False — the
    safe default for a payment-validation gate.
    """
    try:
        a = Decimal(str(actual))
        e = Decimal(str(expected))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return a == e


__all__ = ["amount_matches"]
