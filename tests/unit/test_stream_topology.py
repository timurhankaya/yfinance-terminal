"""Symbol -> connection mapping. The quota tests are load-bearing: Yahoo subscribes a
connection to the first 100 symbols and silently discards the rest."""

from __future__ import annotations

import pytest

from yfin.stream.topology import (
    UNKNOWN_EXCHANGE,
    YAHOO_SUBSCRIPTION_LIMIT,
    ConnectionPlan,
    QuotaExceeded,
    effective_capacity,
    normalise_exchange,
    plan_connections,
    plan_diff,
)


def _universe(count: int, exchange: str = "NMS", prefix: str = "S") -> list[tuple[str, str]]:
    return [(f"{prefix}{i:05d}", exchange) for i in range(count)]


def _plan(pairs: list[tuple[str, str | None]], **kwargs: int) -> list[ConnectionPlan]:
    options: dict[str, int] = {
        "max_symbols_per_connection": 95,
        "max_connections": 256,
        "canary_count": 1,
    }
    options.update(kwargs)
    return plan_connections(pairs, **options)  # type: ignore[arg-type]


# --- the quota -------------------------------------------------------------


def test_no_connection_exceeds_yahoos_limit() -> None:
    """The one invariant that must never break.

    A connection over the limit does not error -- it silently stops
    delivering the symbols past position 100.
    """
    plans = _plan(_universe(10_000))
    for plan in plans:
        assert len(plan.subscription(canary=["BTC-USD"])) <= YAHOO_SUBSCRIPTION_LIMIT


def test_canary_counts_against_the_quota() -> None:
    """Even invalid tickers occupy slots, so the canary does too."""
    assert effective_capacity(canary_count=1) == YAHOO_SUBSCRIPTION_LIMIT - 1
    assert effective_capacity(canary_count=0) == YAHOO_SUBSCRIPTION_LIMIT


def test_per_connection_size_plus_canary_over_the_limit_is_refused() -> None:
    """100 symbols plus a canary is 101, and the 101st is discarded."""
    with pytest.raises(QuotaExceeded, match="discarded silently"):
        _plan(_universe(10), max_symbols_per_connection=100, canary_count=1)


def test_exactly_the_limit_without_a_canary_is_allowed() -> None:
    plans = _plan(_universe(100), max_symbols_per_connection=100, canary_count=0)
    assert len(plans) == 1
    assert len(plans[0].symbols) == 100


# --- splitting -------------------------------------------------------------


def test_ten_thousand_symbols_need_about_a_hundred_connections() -> None:
    """The headline number from the design: 10,000 / 95."""
    plans = _plan(_universe(10_000))
    assert len(plans) == 106
    assert sum(len(p.symbols) for p in plans) == 10_000


def test_a_single_exchange_is_split_across_connections() -> None:
    plans = _plan(_universe(200))
    assert [p.key for p in plans] == ["NMS#0", "NMS#1", "NMS#2"]
    assert [len(p.symbols) for p in plans] == [95, 95, 10]


def test_an_exchange_that_fits_keeps_its_bare_name() -> None:
    """The #n suffix should only appear where it means something."""
    plans = _plan(_universe(5))
    assert [p.key for p in plans] == ["NMS"]


def test_symbols_are_never_lost_in_the_split() -> None:
    pairs = _universe(431)
    plans = _plan(pairs)
    placed = [s for plan in plans for s in plan.symbols]
    assert sorted(placed) == sorted(s for s, _ in pairs)
    assert len(placed) == len(set(placed))


# --- exchange boundary -----------------------------------------------------


def test_one_connection_never_mixes_two_exchanges() -> None:
    """Failure isolation and the Kafka topic layout both rely on this."""
    pairs = _universe(3, "NMS") + _universe(2, "IST", prefix="T") + _universe(1, "ASE", prefix="A")
    plans = _plan(pairs)
    assert [p.exchange for p in plans] == ["ASE", "IST", "NMS"]
    assert all(len({p.exchange}) == 1 for p in plans)


def test_tiny_exchanges_are_not_packed_together() -> None:
    """Fewer connections would be possible; isolation is worth more."""
    pairs = [("A", "X1"), ("B", "X2"), ("C", "X3")]
    assert len(_plan(pairs)) == 3


@pytest.mark.parametrize("exchange", [None, "", "   "])
def test_missing_exchange_groups_under_unknown(exchange: str | None) -> None:
    """`yfin symbols add` leaves exchange NULL until the first sync; those
    symbols still have to be streamed."""
    plans = _plan([("AAPL", exchange)])
    assert plans[0].exchange == UNKNOWN_EXCHANGE


def test_exchange_is_upper_cased() -> None:
    """Raw case differences would otherwise split one exchange in two --
    and, downstream, one Kafka topic into two."""
    assert normalise_exchange("nms") == "NMS"
    plans = _plan([("A", "nms"), ("B", "NMS")])
    assert len(plans) == 1


# --- determinism -----------------------------------------------------------


def test_plan_is_independent_of_input_order() -> None:
    """Yahoo keeps the head of the list, so which symbols survive an
    overflow must not depend on iteration order."""
    pairs = _universe(300)
    forward = _plan(pairs)
    backward = _plan(list(reversed(pairs)))
    assert forward == backward


def test_duplicate_symbols_collapse() -> None:
    plans = _plan([("AAPL", "NMS"), ("AAPL", "NMS")])
    assert plans[0].symbols == ("AAPL",)


# --- subscription order ----------------------------------------------------


def test_canary_goes_last() -> None:
    """At the head it would survive any truncation and report health
    while the tail of the list was being dropped."""
    plan = _plan(_universe(3))[0]
    assert plan.subscription(canary=["BTC-USD"])[-1] == "BTC-USD"


def test_subscription_without_a_canary_is_just_the_symbols() -> None:
    plan = _plan(_universe(3))[0]
    assert plan.subscription() == plan.symbols


# --- the ceiling -----------------------------------------------------------


def test_exceeding_the_connection_ceiling_raises_rather_than_trimming() -> None:
    """Dropping the surplus would look exactly like working correctly."""
    with pytest.raises(QuotaExceeded, match="never dropped"):
        _plan(_universe(1000), max_connections=3)


def test_the_error_says_how_many_connections_are_needed() -> None:
    with pytest.raises(QuotaExceeded) as excinfo:
        _plan(_universe(1000), max_connections=3)
    assert "11 connections needed" in str(excinfo.value)


def test_zero_symbols_per_connection_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _plan(_universe(10), max_symbols_per_connection=0)


def test_empty_universe_plans_nothing() -> None:
    assert _plan([]) == []


# --- rebalancing -----------------------------------------------------------


def test_diff_reports_a_new_exchange_as_one_new_connection() -> None:
    current = _plan(_universe(3, "NMS"))
    desired = _plan(_universe(3, "NMS") + [("XU100", "IST")])
    to_open, to_close, changes = plan_diff(current, desired)
    assert [p.key for p in to_open] == ["IST"]
    assert to_close == []
    assert changes == {}


def test_diff_reports_a_membership_change_without_reopening() -> None:
    """A scope edit must not restart a connection: every reconnect is a
    gap, and re-subscribing re-applies Yahoo's truncation."""
    current = _plan(_universe(3, "NMS"))
    desired = _plan(_universe(4, "NMS"))
    to_open, to_close, changes = plan_diff(current, desired)
    assert to_open == []
    assert to_close == []
    assert changes == {"NMS": ({"S00003"}, set())}


def test_diff_reports_a_removed_exchange() -> None:
    current = _plan(_universe(2, "NMS") + [("XU100", "IST")])
    desired = _plan(_universe(2, "NMS"))
    to_open, to_close, changes = plan_diff(current, desired)
    assert to_close == ["IST"]
    assert to_open == []


def test_identical_plans_produce_no_work() -> None:
    plans = _plan(_universe(50))
    assert plan_diff(plans, plans) == ([], [], {})
