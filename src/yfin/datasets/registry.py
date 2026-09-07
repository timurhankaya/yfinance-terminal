"""Dataset registry and resolver.

Alias expansion, order-preserving dedup, topological sort, cycle and
unknown-name checks all live in one `Registry` class. Three instances are
created: symbol-scoped (`SYMBOL_DATASETS`), market-scoped
(`MARKET_DATASETS`), and sector/industry-scoped (`DOMAIN_DATASETS`)
datasets. All three share the same contract; the only difference is
whether a bootstrap dataset exists.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from yfin.models.bars import BAR_INTERVALS, INTRADAY_INTERVALS

if TYPE_CHECKING:
    from yfin.datasets.base import Dataset
    from yfin.datasets.domain.base import DomainDataset
    from yfin.datasets.market.base import GlobalDataset


class Registrable(Protocol):
    """The only interface the registry sees."""

    name: str
    depends_on: tuple[str, ...]


class UnknownDatasetError(ValueError):
    pass


class DependencyCycleError(ValueError):
    pass


class Registry[D: Registrable]:
    """Name -> dataset map and resolver.

    If `bootstrap` is given, that dataset is always prepended to every
    resolution even if the user doesn't select it (`symbols` on the symbol
    side). `bootstrap=None` is for the market side: there is no mandatory
    prerequisite dataset.
    """

    def __init__(
        self,
        *,
        bootstrap: str | None = None,
        aliases: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.bootstrap = bootstrap
        self.aliases: dict[str, tuple[str, ...]] = dict(aliases or {})
        self._items: dict[str, D] = {}
        # Datasets that never run UNLESS NAMED EXPLICITLY. The opposite of
        # `bootstrap`: that one gets ADDED to every resolution, these get
        # EXCLUDED from `all`.
        self._opt_in: set[str] = set()

    # --- registration -------------------------------------------------

    def register(self, ds: D, *, opt_in: bool = False) -> D:
        """`opt_in=True`: registered but NOT INCLUDED in the `all` expansion.

        The reasoning is a measured trap. `search` and `lookup` each add one
        request per symbol; across 4,500 symbols that's +9,000 requests/day.
        If registered unconditionally, a bare `yfin sync` would pull them too.

        The FIRST FIX WAS WRONG: gating registration behind a setting (the
        `sustainability` pattern). That also made the dataset unreachable
        via `--datasets search`, and flipping the setting on made a bare run
        expensive again -- it didn't solve the trap, just postponed it.

        `opt_in` solves both: it runs when named, and is INVISIBLE in the
        `all` expansion. The declaration stays at the dataset's
        REGISTRATION SITE, not in a name list embedded in the registry.
        """
        # Read reflectively rather than through the protocol: the market
        # and domain dataset hierarchies are registrable without being
        # exposable, and widening the protocol would force both to carry
        # fields they never use.
        for exposure in getattr(ds, "api", ()):
            # Validated here, at import time. A misdeclared dataset should
            # stop the process from starting rather than surface as a 500
            # to whoever calls it first.
            exposure.validate(
                dataset_name=ds.name, produces=tuple(getattr(ds, "produces", ()))
            )
        self._items[ds.name] = ds
        if opt_in:
            self._opt_in.add(ds.name)
        else:
            self._opt_in.discard(ds.name)
        return ds

    def is_opt_in(self, name: str) -> bool:
        return name in self._opt_in

    def unregister(self, name: str) -> None:
        """Test-only; an unregistered name is silently ignored."""
        self._items.pop(name, None)
        self._opt_in.discard(name)

    # Collection protocol: `name in registry`, `registry[name]`,
    # `len(registry)`, `for name in registry`. A separate names()/values()
    # would just open a second face onto the same data.
    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __getitem__(self, name: str) -> D:
        return self._items[name]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def get(self, name: str) -> D | None:
        return self._items.get(name)

    def user_visible_names(self) -> list[str]:
        """Names the user can pass to --datasets: registrations (excluding
        bootstrap) + aliases."""
        visible = set(self._items) | set(self.aliases)
        if self.bootstrap is not None:
            visible -= {self.bootstrap}
        return sorted(visible)

    # --- resolution -----------------------------------------------------

    def _expand(self, names: Sequence[str]) -> list[str]:
        """Expands aliases, deduplicates while PRESERVING ORDER."""
        out: dict[str, None] = {}
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            if name in self.aliases:
                for target in self.aliases[name]:
                    out[target] = None
                continue
            out[name] = None
        return list(out)

    def resolve(self, names: Sequence[str] | None) -> list[D]:
        """None, empty list, or 'all' -> everything that is NOT opt-in.

        Expands aliases, deduplicates preserving order, topologically sorts
        by depends_on, rejects unknown names, catches cycles. If bootstrap
        is defined, it is always prepended.
        """
        if names is None or not names or (len(names) == 1 and names[0].strip() == "all"):
            selected = [
                n for n in self._items if n != self.bootstrap and n not in self._opt_in
            ]
        else:
            selected = self._expand(names)

        unknown = [n for n in selected if n not in self._items]
        if unknown:
            raise UnknownDatasetError(
                f"unknown dataset: {', '.join(unknown)}. "
                f"valid names: {', '.join(self.user_visible_names())}"
            )

        wanted: dict[str, None] = {}
        if self.bootstrap is not None:
            wanted[self.bootstrap] = None
        for name in selected:
            wanted[name] = None

        ordered: list[str] = []
        state: dict[str, int] = {}  # 0=visiting, 1=done

        def visit(name: str, path: tuple[str, ...]) -> None:
            if state.get(name) == 1:
                return
            if state.get(name) == 0:
                raise DependencyCycleError(f"cycle: {' -> '.join((*path, name))}")
            if name not in self._items:
                raise UnknownDatasetError(f"unknown dependency: {name}")
            state[name] = 0
            for dep in self._items[name].depends_on:
                visit(dep, (*path, name))
            state[name] = 1
            ordered.append(name)

        for name in wanted:
            visit(name, ())

        return [self._items[n] for n in ordered]


# --- instances --------------------------------------------------------

# 'actions' and 'financials' are ALIASES in the registry, not registrations.
SYMBOL_DATASETS: Registry[Dataset[Any]] = Registry(
    bootstrap="symbols",
    aliases={
        "actions": ("dividends", "splits", "capital_gains"),
        # price_bars dataset family. DERIVED from the interval set, not
        # retyped: a listed-by-hand alias that missed a new interval would
        # register it and then silently skip it under `--datasets bars`.
        # The set `bars` expands to can be narrowed with YF_BAR_INTERVALS;
        # the registrations themselves are always all of them.
        "bars": tuple(f"bars_{i}" for i in BAR_INTERVALS),
        "intraday": tuple(f"bars_{i}" for i in INTRADAY_INTERVALS),
        # `recommendations_summary` is an ALIAS, not a registration: in the
        # source its body is `return self.get_recommendations(as_dict=as_dict)`
        # (base.py:220).
        "recommendations_summary": ("recommendations",),
        # `sustainability` is NOT INCLUDED: it's a monitoring dataset with
        # no table, and is never registered by default.
        "analysis": (
            "recommendations",
            "upgrades_downgrades",
            "analyst_price_targets",
            "earnings_estimate",
            "revenue_estimate",
            "eps_trend",
            "eps_revisions",
            "earnings_history",
            "growth_estimates",
        ),
        "holders": (
            "major_holders",
            "institutional_holders",
            "mutualfund_holders",
            "insider_purchases",
            "insider_transactions",
            "insider_roster_holders",
        ),
        "funds": ("funds_data",),
        # `valuation` is a SEPARATE alias, NOT part of `financials`: it does
        # not appear under the source docs' Financials section and is a
        # separate HTTP request; folding it into `financials` would silently
        # double that name's existing cost.
        "valuation": ("valuation_measures", "quarterly_valuation_measures"),
        "financials": (
            "income_stmt",
            "quarterly_income_stmt",
            "ttm_income_stmt",
            "balance_sheet",
            "quarterly_balance_sheet",
            "cashflow",
            "quarterly_cashflow",
            "ttm_cashflow",
        ),
    },
)

MARKET_DATASETS: Registry[GlobalDataset[Any]] = Registry(
    bootstrap=None,
    aliases={
        "market": ("market_status", "market_summary"),
        "calendars": (
            "earnings_calendar",
            "economic_calendar",
            "ipo_calendar",
            "splits_calendar",
        ),
    },
)


# Third registry. `Registry` itself is UNCHANGED: the `Registrable`
# protocol needs `name` + `depends_on`, and `DomainDataset` carries both.
DOMAIN_DATASETS: Registry[DomainDataset[Any]] = Registry(
    bootstrap="domain_taxonomy",
    aliases={
        "sector": ("sector_profile", "sector_rankings"),
        "industry": ("industry_profile", "industry_rankings"),
    },
)


def register(ds: Dataset[Any], *, opt_in: bool = False) -> Dataset[Any]:
    """Registers a symbol-scoped dataset (used as a decorator in dataset modules).

    `opt_in=True` -> excluded from the `all` expansion; see `Registry.register`.
    """
    return SYMBOL_DATASETS.register(ds, opt_in=opt_in)


def register_market(ds: GlobalDataset[Any], *, opt_in: bool = False) -> GlobalDataset[Any]:
    """Registers a market-scoped dataset."""
    return MARKET_DATASETS.register(ds, opt_in=opt_in)


def register_domain(ds: DomainDataset[Any]) -> DomainDataset[Any]:
    """Registers a sector / industry-scoped dataset."""
    return DOMAIN_DATASETS.register(ds)
