"""Retroactive rescaling - pure computation.

No SQL: direction, type, and boundary rules are validated without touching
the DB. Idempotency and atomicity are covered by the repo tests.

The formula's DIRECTION rests on a measured fact: after a split, Yahoo
rescales the ENTIRE history - it divides price and multiplies volume (NVDA
2024-06-10 10:1; 2024-06-05 Close=122.44 / Volume=528,402,000, when in
reality it was ~1224.40 and ~52.84 M). Old rows in our archive are at the
pre-split scale; the UPDATE aligns them to Yahoo's current scale.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from yfin.storage.rescale import RescaleSkipped, rescale_factors, split_boundary_utc


def test_forward_split_divides_price_and_multiplies_volume() -> None:
    """NVDA 10:1 - measured direction."""
    price_factor, volume_factor = rescale_factors(Decimal(10))

    assert Decimal("1224.40") * price_factor == Decimal("122.440")
    assert 52_840_210 * volume_factor == Decimal("528402100")


def test_reverse_split_uses_the_same_formula() -> None:
    """At ratio < 1, division turns into multiplication; there is NO special branch."""
    price_factor, volume_factor = rescale_factors(Decimal("0.1"))

    assert Decimal("12.244") * price_factor == Decimal("122.44")
    assert Decimal("528402100") * volume_factor == Decimal("52840210.0")


def test_fractional_ratio_stays_decimal() -> None:
    """3:2 split ratio=1.5; float division produces cumulative drift across
    millions of rows."""
    price_factor, _ = rescale_factors(Decimal("1.5"))
    result = Decimal("150") * price_factor

    assert isinstance(result, Decimal)
    assert result == Decimal(100)


@pytest.mark.parametrize("bad", [Decimal(0), Decimal("-2")])
def test_non_positive_ratio_is_rejected(bad: Decimal) -> None:
    """A broken 0 row in splits would produce ERROR_FOR_DIVISION_BY_ZERO and
    drop the ENTIRE symbol."""
    with pytest.raises(RescaleSkipped):
        rescale_factors(bad)


# --- split boundary ---------------------------------------------------------


def test_split_boundary_is_local_midnight_in_utc() -> None:
    """AAPL: America/New_York -04:00 -> local 00:00 = UTC 04:00."""
    boundary = split_boundary_utc(date(2026, 6, 10), "America/New_York")

    assert boundary == datetime(2026, 6, 10, 4, 0, tzinfo=UTC)


def test_positive_offset_exchange_shifts_the_other_way() -> None:
    """BIST +03: local 00:00 = the PREVIOUS day's UTC 21:00.

    If raw UTC midnight were used, bars between 00:00-03:00 on the split day
    (no session on BIST, but there is one for crypto/futures) would end up on
    the wrong side.
    """
    boundary = split_boundary_utc(date(2026, 6, 10), "Europe/Istanbul")

    assert boundary == datetime(2026, 6, 9, 21, 0, tzinfo=UTC)


def test_dst_transition_is_handled_by_zoneinfo() -> None:
    """During winter time the offset reverts to -05:00."""
    assert split_boundary_utc(date(2026, 1, 15), "America/New_York") == datetime(
        2026, 1, 15, 5, 0, tzinfo=UTC
    )


def test_unknown_timezone_is_skipped_not_guessed() -> None:
    """If tz is unknown, UTC is NOT assumed: rescaling with the wrong boundary
    is worse than not rescaling at all - the result cannot be undone."""
    with pytest.raises(RescaleSkipped):
        split_boundary_utc(date(2026, 6, 10), None)
    with pytest.raises(RescaleSkipped):
        split_boundary_utc(date(2026, 6, 10), "EDT")


def test_zoneinfo_accepts_the_iana_names_we_store() -> None:
    """history_metadata.exchange_timezone_name must really be an IANA name;
    the `timezone` column ("EDT"/"TRT") CANNOT be used."""
    for name in ("America/New_York", "Europe/Istanbul", "Europe/London", "UTC"):
        assert ZoneInfo(name) is not None
