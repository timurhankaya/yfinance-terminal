"""Which symbol is listened to on which connection.

Pure functions over a symbol list: no sockets, no database, no clock.
That is deliberate -- the quota rule below is the single place where
silent data loss can enter this subsystem, so it has to be testable
without any of that.

The rule it enforces was measured, not assumed. Yahoo subscribes a
connection to **exactly 100 symbols** and silently discards the rest:
101 symbols means one of them never arrives, with no error frame, no
close, and no acknowledgement. A client can believe it is watching 10,000
symbols while receiving 100.

Everything here follows from that:

  * connections are sized, not balanced -- an exchange with 5 symbols and
    one with 698 both stay under the quota, and that is the only property
    that matters;
  * the ordering is deterministic, because the server keeps the first 100
    entries and drops the tail, so *which* symbols survive must not
    depend on set iteration order or PYTHONHASHSEED;
  * exceeding the connection ceiling raises instead of dropping symbols.
    Silently trimming here would repeat, on our side, exactly the
    mistake that makes Yahoo's behaviour dangerous.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

#: Measured ceiling: a connection is subscribed to the first 100 symbols
#: of whatever it sends, and the remainder is discarded without notice.
#: See docs/measurements/websocket.md.
YAHOO_SUBSCRIPTION_LIMIT: Final = 100

#: Bucket for symbols whose exchange is unknown. The same label is used
#: for the Kafka topic, so it has to be a valid topic segment: no
#: underscores next to dots, no leading punctuation.
UNKNOWN_EXCHANGE: Final = "unknown"


class QuotaExceeded(RuntimeError):
    """The plan would need more connections than the ceiling allows.

    Raised rather than trimmed. The alternative -- dropping the symbols
    that do not fit -- would be indistinguishable from working correctly
    until someone noticed a missing series weeks later.
    """


@dataclass(frozen=True)
class ConnectionPlan:
    """One upstream connection: its key, its exchange and its symbols.

    `symbols` excludes the canary. The canary is appended at send time by
    the connection itself, because it is an instrument rather than data:
    it is never archived and never appears in `stream_scope`.
    """

    key: str
    exchange: str
    symbols: tuple[str, ...]

    def subscription(self, canary: Sequence[str] = ()) -> tuple[str, ...]:
        """What actually goes on the wire, canary last.

        Last, not first, and this is the whole point: Yahoo truncates
        from the front, so a canary at the head would survive any
        overflow and report health while the tail of the list was being
        discarded. At the end it goes silent exactly when symbols start
        being dropped.
        """
        return (*self.symbols, *canary)


def effective_capacity(canary_count: int, limit: int = YAHOO_SUBSCRIPTION_LIMIT) -> int:
    """How many real symbols fit once the canary has taken its slot.

    The canary consumes quota like any other symbol -- measured: invalid
    and unknown tickers occupy slots too, so nothing about it is free.
    """
    return limit - canary_count


def normalise_exchange(exchange: str | None) -> str:
    """`symbols.exchange` -> a grouping key.

    NULL is a real state here: `yfin symbols add` writes only the symbol
    and is_active, so exchange stays NULL until the first sync. Those
    symbols still have to be streamed, they just group together.
    """
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

    `symbols_by_exchange` is (symbol, exchange) as read from the scope
    join. Order of the input does not matter; the output is sorted so two
    processes given the same universe produce byte-identical
    subscriptions.

    One connection never carries two exchanges. Packing small exchanges
    together would use fewer connections, but it would also mean a
    failure on one exchange takes down symbols from another -- and the
    Kafka topic layout is per-exchange, so the boundary has to survive
    anyway.
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

    Rebalancing is surgical on purpose: a scope edit must not restart
    connections whose membership did not change. Every reconnect is a
    gap in the archive, and re-sending a subscription re-applies Yahoo's
    truncation.
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
