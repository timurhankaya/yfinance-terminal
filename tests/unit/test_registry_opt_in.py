"""`opt_in` -- a dataset that is registered but excluded from `all`.

This concept closes a trap in the original design. There, `search`/`lookup`
registration was gated behind a setting (the `sustainability` pattern),
which had two flaws:

  1. With the setting off, `--datasets search` also failed: the dataset
     didn't exist in the registry at all, and the user saw "unknown dataset".
  2. The moment the setting was turned on, a bare `yfin sync` started
     fetching them too -- +9,000 requests/day across 4,500 symbols. So the
     flag didn't solve the trap, it only postponed it until the user
     flipped it on.

`opt_in` solves both and is the exact inverse of `bootstrap`: `bootstrap`
is added to every resolution, `opt_in` is excluded from `all`.
"""

from __future__ import annotations

from typing import Any

import pytest

from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import Registry


class _Ds(Dataset[None]):
    def __init__(self, name: str) -> None:
        self.name = name

    def fetch(self, ctx: SyncContext) -> None:  # pragma: no cover
        return None

    def normalize(self, raw: None, symbol: str) -> NormalizedResult:  # pragma: no cover
        return NormalizedResult()


@pytest.fixture
def registry() -> Registry[Any]:
    reg: Registry[Any] = Registry(bootstrap="symbols")
    reg.register(_Ds("symbols"))
    reg.register(_Ds("info"))
    reg.register(_Ds("search"), opt_in=True)
    return reg


def _names(datasets: list[Any]) -> list[str]:
    return [d.name for d in datasets]


class TestAllExpansion:
    def test_opt_in_is_excluded_from_none(self, registry: Registry[Any]) -> None:
        assert _names(registry.resolve(None)) == ["symbols", "info"]

    def test_opt_in_is_excluded_from_literal_all(self, registry: Registry[Any]) -> None:
        assert "search" not in _names(registry.resolve(["all"]))

    def test_opt_in_is_excluded_from_empty_list(self, registry: Registry[Any]) -> None:
        assert "search" not in _names(registry.resolve([]))


class TestExplicitRequest:
    def test_named_opt_in_runs(self, registry: Registry[Any]) -> None:
        """The second half of the trap: it must run when requested by name.

        If registration were gated by a setting, this would raise
        `UnknownDatasetError`.
        """
        assert _names(registry.resolve(["search"])) == ["symbols", "search"]

    def test_opt_in_stays_user_visible(self, registry: Registry[Any]) -> None:
        """Visible in `yfin datasets` output: the user cannot request it
        without knowing its name."""
        assert "search" in registry.user_visible_names()

    def test_is_opt_in_reports_the_flag(self, registry: Registry[Any]) -> None:
        assert registry.is_opt_in("search") is True
        assert registry.is_opt_in("info") is False


class TestRegistrationHygiene:
    def test_reregistering_without_flag_clears_it(self, registry: Registry[Any]) -> None:
        """Re-registering the same name without opt_in clears the flag.

        Otherwise a name once marked opt_in would silently stay opt_in on
        the next registration, and `all` would expand narrower than expected.
        """
        registry.register(_Ds("search"))
        assert registry.is_opt_in("search") is False
        assert "search" in _names(registry.resolve(None))

    def test_unregister_clears_the_flag(self, registry: Registry[Any]) -> None:
        registry.unregister("search")
        registry.register(_Ds("search"))
        assert registry.is_opt_in("search") is False

    def test_bootstrap_is_still_prepended(self, registry: Registry[Any]) -> None:
        """`opt_in` does not affect `bootstrap`: one adds, the other excludes."""
        assert _names(registry.resolve(["search"]))[0] == "symbols"


class TestRealRegistries:
    def test_bare_sync_excludes_discovery(self) -> None:
        """Measured trap: if these two datasets were in `all`, that would
        add +9,000 requests/day across 4,500 symbols."""
        from yfin.datasets.registry import SYMBOL_DATASETS

        names = _names(SYMBOL_DATASETS.resolve(None))
        assert "search" not in names
        assert "lookup" not in names

    def test_named_discovery_resolves(self) -> None:
        from yfin.datasets.registry import SYMBOL_DATASETS

        assert set(_names(SYMBOL_DATASETS.resolve(["search", "lookup"]))) == {
            "symbols",
            "search",
            "lookup",
        }

    def test_bare_market_sync_excludes_screener(self) -> None:
        """`yfin market sync`'s cost is unchanged: fetching screener would
        push the command from ~20 requests to ~50."""
        from yfin.datasets.registry import MARKET_DATASETS

        assert "screener" not in _names(MARKET_DATASETS.resolve(None))
        assert _names(MARKET_DATASETS.resolve(["screener"])) == ["screener"]
