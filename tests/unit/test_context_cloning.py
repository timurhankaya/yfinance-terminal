"""`_clone` must carry every field, including ones added after it was written. Driven off
`dataclasses.fields()`, so a new field is covered without anyone editing this file; the
author only has to supply a value to change it to."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from typing import Any

import pytest

from yfin.datasets.domain.base import DomainContext
from yfin.datasets.market.base import MarketContext

FETCHED_AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

#: A value different from the one the fixture below carries, per field. The
#: cache is excluded: it is shared by identity, which is its own test.
MARKET_SENTINELS: dict[str, Any] = {
    "fetched_at": datetime(2000, 1, 1, tzinfo=UTC),
    "start": date(2000, 1, 1),
    "end": date(2000, 12, 31),
    "region": "ASIA",
    "variant": "small_caps",
}

DOMAIN_SENTINELS: dict[str, Any] = {
    "fetched_at": datetime(2000, 1, 1, tzinfo=UTC),
    "as_of_date": date(2000, 1, 1),
    "primary_region": "ASIA",
    "region": "JAPAN",
    "key": "healthcare",
    "domain_type": "industry",
    "parents": {"semiconductors": "technology"},
}


def _field_names(cls: type) -> list[str]:
    return [f.name for f in dataclasses.fields(cls) if f.name != "_cache"]


def _carried(context: Any) -> dict[str, Any]:
    return {name: getattr(context, name) for name in _field_names(type(context))}


def _market() -> MarketContext:
    return MarketContext(
        fetched_at=FETCHED_AT,
        start=date(2026, 9, 1),
        end=date(2026, 10, 1),
        region="EUROPE",
        variant="day_gainers",
    )


def _domain() -> DomainContext:
    return DomainContext(
        fetched_at=FETCHED_AT,
        as_of_date=date(2026, 9, 1),
        primary_region="US",
        region="EUROPE",
        key="technology",
        domain_type="sector",
        parents={"a": "b"},
    )


class TestEveryFieldIsCovered:
    """The guard that makes the two suites below self-maintaining."""

    def test_market_sentinels_name_every_field(self) -> None:
        missing = set(_field_names(MarketContext)) - set(MARKET_SENTINELS)
        assert not missing, (
            f"MarketContext gained {sorted(missing)}; add a value to MARKET_SENTINELS "
            "so the cloning tests cover it"
        )

    def test_domain_sentinels_name_every_field(self) -> None:
        missing = set(_field_names(DomainContext)) - set(DOMAIN_SENTINELS)
        assert not missing, (
            f"DomainContext gained {sorted(missing)}; add a value to DOMAIN_SENTINELS "
            "so the cloning tests cover it"
        )


class TestMarketContext:
    def test_clone_carries_every_field_it_was_not_asked_to_change(self) -> None:
        base = _market()
        assert _carried(base._clone()) == _carried(base)

    @pytest.mark.parametrize("name", MARKET_SENTINELS)
    def test_every_field_can_actually_be_changed(self, name: str) -> None:
        """`_clone(**changes)` honours whatever field it is handed, not just some of them."""
        clone = _market()._clone(**{name: MARKET_SENTINELS[name]})
        assert getattr(clone, name) == MARKET_SENTINELS[name]

    def test_an_unknown_field_is_refused(self) -> None:
        """A typo in a change key must not be silently ignored."""
        with pytest.raises(TypeError):
            _market()._clone(regoin="US")

    def test_clones_share_the_cache(self) -> None:
        base = _market()
        assert base._clone()._cache is base._cache


class TestDomainContext:
    def test_clone_carries_every_field_it_was_not_asked_to_change(self) -> None:
        base = _domain()
        assert _carried(base._clone()) == _carried(base)

    @pytest.mark.parametrize("name", DOMAIN_SENTINELS)
    def test_every_field_can_actually_be_changed(self, name: str) -> None:
        clone = _domain()._clone(**{name: DOMAIN_SENTINELS[name]})
        assert getattr(clone, name) == DOMAIN_SENTINELS[name]

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(TypeError):
            _domain()._clone(domian_type="industry")

    def test_clones_share_the_cache(self) -> None:
        base = _domain()
        assert base._clone()._cache is base._cache

    def test_parents_stays_the_same_object(self) -> None:
        """The runner fills `parents` once after the taxonomy pass and every
        later context reads it; a copy per clone would leave the industry
        datasets comparing against an empty map."""
        base = _domain()
        assert base.for_region("US").parents is base.parents
