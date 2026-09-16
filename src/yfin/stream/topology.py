"""Which symbol is listened to on which connection.

Pure functions, no I/O. Yahoo keeps the first 100 symbols and silently
drops the rest, so ordering is deterministic and overflow raises, never trims.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

#: A connection is subscribed to the first 100 symbols it sends; the
#: remainder is discarded without notice.
YAHOO_SUBSCRIPTION_LIMIT: Final = 100

#: Bucket for symbols whose exchange is unknown. The same label is used
#: for the Kafka topic, so it has to be a valid topic segment: no
#: underscores next to dots, no leading punctuation.
UNKNOWN_EXCHANGE: Final = "unknown"


class QuotaExceeded(RuntimeError):
    """The plan would need more connections than the ceiling allows; never trimmed."""


@dataclass(frozen=True)
class ConnectionPlan:
    """One upstream connection. `symbols` excludes the canary, appended at send time."""

    key: str
    exchange: str
    symbols: tuple[str, ...]

    def subscription(self, canary: Sequence[str] = ()) -> tuple[str, ...]:
        """What goes on the wire, canary last.

        Yahoo keeps the head of the list, so a canary at the end goes
        silent exactly when symbols start being dropped.
        """
        return (*self.symbols, *canary)


def effective_capacity(canary_count: int, limit: int = YAHOO_SUBSCRIPTION_LIMIT) -> int:
    """How many real symbols fit once the canary has taken its quota slot."""
    return limit - canary_count


def normalise_exchange(exchange: str | None) -> str:
    """`symbols.exchange` -> a grouping key; NULL (unsynced) symbols group together."""
    if exchange is None:
        return UNKNOWN_EXCHANGE
    cleaned = exchange.strip().upper()
    return cleaned or UNKNOWN_EXCHANGE


def plan_connections(
    symbols_by_exchange: Iterable[tuple[str, str | None]],
    *,
    max_symbols_per_connection: int,
    max_connections: int,
    canary_count: int = 0,
) -> list[ConnectionPlan]:
    """Groups symbols into connections that respect Yahoo's quota.

    Output is sorted so the same universe yields identical subscriptions.
    One connection never carries two exchanges (Kafka topics are per-exchange).
    """
    if max_symbols_per_connection <= 0:
        raise ValueError("max_symbols_per_connection must be positive")

    capacity = effective_capacity(canary_count)
    if max_symbols_per_connection > capacity:
        raise QuotaExceeded(
            f"max_symbols_per_connection={max_symbols_per_connection} plus "
            f"{canary_count} canary symbol(s) exceeds Yahoo's limit of "
            f"{YAHOO_SUBSCRIPTION_LIMIT}; the surplus would be discarded silently"
        )

    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for symbol, exchange in symbols_by_exchange:
        grouped[normalise_exchange(exchange)].append(symbol)

    plans: list[ConnectionPlan] = []
    for exchange in sorted(grouped):
        # Sorted, and sorted again inside the split: the server keeps the
        # head of the list, so a non-deterministic order would change
        # which symbols survive an overflow between restarts.
        members = sorted(set(grouped[exchange]))
        parts = math.ceil(len(members) / max_symbols_per_connection)
        for index in range(parts):
            chunk = members[
                index * max_symbols_per_connection : (index + 1) * max_symbols_per_connection
            ]
            # An exchange that fits in one connection keeps its bare name;
            # the #n suffix appears only where it means something.
            key = exchange if parts == 1 else f"{exchange}#{index}"
            plans.append(ConnectionPlan(key=key, exchange=exchange, symbols=tuple(chunk)))

    if len(plans) > max_connections:
        needed = len(plans)
        raise QuotaExceeded(
            f"{needed} connections needed for {sum(len(p.symbols) for p in plans)} symbols "
            f"at {max_symbols_per_connection} per connection, but "
            f"max_connections={max_connections}. Raise the ceiling or narrow the scope; "
            f"symbols are never dropped to fit."
        )
    return plans


def plan_diff(
    current: Sequence[ConnectionPlan], desired: Sequence[ConnectionPlan]
) -> tuple[list[ConnectionPlan], list[str], dict[str, tuple[set[str], set[str]]]]:
    """What changed between two plans.

    Returns (connections to open, keys to close, per-key add/remove sets).
    Connections whose membership did not change are left alone.
    """
    current_by_key = {plan.key: plan for plan in current}
    desired_by_key = {plan.key: plan for plan in desired}

    to_open = [desired_by_key[key] for key in sorted(desired_by_key.keys() - current_by_key)]
    to_close = sorted(current_by_key.keys() - desired_by_key)

    changes: dict[str, tuple[set[str], set[str]]] = {}
    for key in sorted(current_by_key.keys() & desired_by_key.keys()):
        before = set(current_by_key[key].symbols)
        after = set(desired_by_key[key].symbols)
        added, removed = after - before, before - after
        if added or removed:
            changes[key] = (added, removed)
    return to_open, to_close, changes
