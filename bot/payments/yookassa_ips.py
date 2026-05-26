"""IP allowlist for the YooKassa webhook endpoint.

YooKassa publishes the IP ranges from which they send `payment.*` notifications
in their developer docs. Restricting `/yookassa/webhook` to those ranges is a
cheap layer of defense-in-depth on top of the existing "re-fetch the payment
by id" verification: it stops scanners from forcing us to make YooKassa API
calls for every probe, and it stops a leaked webhook URL from being abused.

Operators opt in via the `YOOKASSA_WEBHOOK_IP_ALLOWLIST` env var:

* unset / empty           — allowlist is OFF (any source IP is accepted)
* `default`               — use the published list baked in below
* `1.2.3.0/24,5.6.7.8`    — explicit comma-separated CIDRs / individual hosts
                            (a bare IP is treated as /32 or /128)

The list below is current as of mid-2026; bump it in PRs when YooKassa updates
their docs. Operators who can't redeploy can override the env var without
touching code.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# Source: https://yookassa.ru/developers/using-api/webhooks
DEFAULT_YOOKASSA_CIDRS: tuple[str, ...] = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "2a02:5180:0:1509::/64",
    "2a02:5180:0:2655::/64",
    "2a02:5180:0:1533::/64",
    "2a02:5180:0:2669::/64",
)


def parse_allowlist(spec: str | None) -> list[ipaddress._BaseNetwork]:
    """Parse the env-supplied spec into a list of `ip_network` objects.

    Returns an empty list when the allowlist is disabled (unset/empty). Unknown
    entries are logged and skipped — a single typo in the env var should not
    blackhole the entire webhook endpoint.
    """
    if not spec:
        return []
    raw = spec.strip()
    if not raw:
        return []
    if raw.lower() == "default":
        entries: Iterable[str] = DEFAULT_YOOKASSA_CIDRS
    else:
        entries = [part.strip() for part in raw.split(",") if part.strip()]

    networks: list[ipaddress._BaseNetwork] = []
    for entry in entries:
        try:
            # `strict=False` so "1.2.3.4" without a /mask becomes 1.2.3.4/32.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid CIDR/IP in allowlist: %r", entry)
    return networks


def ip_is_allowed(ip: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    """Return True iff `ip` falls inside ANY of `networks`.

    An empty `networks` list means "allowlist disabled" → always True.
    Malformed `ip` values fail closed (return False) so a missing/garbage
    source address can't slip past a configured allowlist.
    """
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    return any(addr in net for net in networks)


__all__ = ["DEFAULT_YOOKASSA_CIDRS", "ip_is_allowed", "parse_allowlist"]
