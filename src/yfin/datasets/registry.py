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

from yfin.datasets.exposure import ApiExposure
from yfin.models.bars import BAR_INTERVALS, INTRADAY_INTERVALS

if TYPE_CHECKING:
    from yfin.datasets.base import Dataset
    from yfin.datasets.domain.base import DomainDataset
    from yfin.datasets.market.base import GlobalDataset


class Registrable(Protocol):
    """The only interface the registry sees.

    All four fields, because the registry reads all four. It used to
    declare two and reach for the other two with `getattr(ds, ..., ())`,
    on the argument that market and domain datasets were registrable
    without being exposable. That stopped being true: five of seven market
    datasets and four of five domain ones declare `api`. What the
    reflective read bought was a typo that compiled -- `apis = (...)`
    instead of `api = (...)` created a new attribute, passed mypy, skipped
    `validate()` entirely and left the resource out of the catalogue with
    nothing anywhere to say why.
    """

    name: str
    depends_on: tuple[str, ...]
    produces: tuple[str, ...]
    api: tuple[ApiExposure, ...]


#: The name that expands to every dataset that is not opt-in.
ALL = "all"


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
        #: Aliases whose members are genuinely arbitrary -- a rename, a
        #: convenience grouping, a pair of names that happen to belong
        #: together. A family that is simply "every dataset of this kind"
        #: is NOT written here; see `register(family=...)`.
        self.explicit_aliases: dict[str, tuple[str, ...]] = dict(aliases or {})
        #: Families, collected from the registration sites.
        self._families: dict[str, list[str]] = {}
        self._items: dict[str, D] = {}
        # Datasets that never run UNLESS NAMED EXPLICITLY. The opposite of
        # `bootstrap`: that one gets ADDED to every resolution, these get
        # EXCLUDED from `all`.
        self._opt_in: set[str] = set()

    # --- registration -------------------------------------------------

    @property
    def aliases(self) -> dict[str, tuple[str, ...]]:
        """Every name that expands to several: explicit ones and families.

        A family cannot shadow a dataset or an explicit alias -- that is
        checked at registration -- so merging them is unambiguous.
        """
        return {
            **{name: tuple(members) for name, members in self._families.items()},
            **self.explicit_aliases,
        }

    def register(self, ds: D, *, opt_in: bool = False, family: str | None = None) -> D:
        """Both flags are declared here, at the registration site.

        `family="financials"` puts the dataset in the `--datasets financials`
        group. Declared here rather than listed in this module, for the
        reason the `bars` alias already gives: a hand-written list that
        misses a new member registers it and then silently skips it. `bars`
        derived its members and three neighbouring families did not, so
        adding a ninth statement would have registered it, made it reachable
        by name, included it in `all`, and left `--datasets financials`
        quietly without it -- no error, no log.

        `opt_in=True`: registered but NOT INCLUDED in the `all` expansion.
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
        for exposure in ds.api:
            # Validated here, at import time. A misdeclared dataset should
            # stop the process from starting rather than surface as a 500
            # to whoever calls it first.
            exposure.validate(dataset_name=ds.name, produces=ds.produces)
        self._items[ds.name] = ds
        if opt_in:
            self._opt_in.add(ds.name)
        else:
            self._opt_in.discard(ds.name)
        if family is not None:
            if family in self.explicit_aliases:
                raise ValueError(
                    f"{ds.name}: family {family!r} is already an explicit alias"
                )
            members = self._families.setdefault(family, [])
            if ds.name not in members:
                members.append(ds.name)
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
        visible = set(self._items) | set(self.aliases) | {ALL}
        if self.bootstrap is not None:
            visible -= {self.bootstrap}
        return sorted(visible)

    # --- resolution -----------------------------------------------------

    def _default_set(self) -> list[str]:
        """Everything that is neither the bootstrap nor opt-in."""
        return [n for n in self._items if n != self.bootstrap and n not in self._opt_in]

    def _expand(self, names: Sequence[str]) -> list[str]:
        """Expands aliases, deduplicates while PRESERVING ORDER."""
        out: dict[str, None] = {}
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            if name == ALL:
                # An ordinary expansion, not a special case. It used to be
                # recognised only when it stood alone, so `--datasets
                # all,search` -- the natural way to add an opt-in dataset to
                # the usual set -- failed as an unknown name, and the list
                # of valid names in the error did not contain `all` either.
                for target in self._default_set():
                    out[target] = None
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
        selected = self._default_set() if not names else self._expand(names)

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
        # `analysis`, `holders` and `financials` are FAMILIES now, declared
        # at each dataset's registration site rather than listed here.
        # `sustainability` simply declares none: it is a monitoring dataset
        # with no table, and its exclusion is now visible where it is
        # registered instead of by its absence from a list in this file.
        "funds": ("funds_data",),
        # `valuation` is a SEPARATE alias, NOT part of `financials`: it does
        # not appear under the source docs' Financials section and is a
        # separate HTTP request; folding it into `financials` would silently
        # double that name's existing cost.
        "valuation": ("valuation_measures", "quarterly_valuation_measures"),
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


def register(
    ds: Dataset[Any], *, opt_in: bool = False, family: str | None = None
) -> Dataset[Any]:
    """Registers a symbol-scoped dataset (used as a decorator in dataset modules).

    `opt_in=True` -> excluded from the `all` expansion; `family=` puts it in
    a `--datasets <family>` group. See `Registry.register`.
    """
    return SYMBOL_DATASETS.register(ds, opt_in=opt_in, family=family)


def register_market(
    ds: GlobalDataset[Any], *, opt_in: bool = False, family: str | None = None
) -> GlobalDataset[Any]:
    """Registers a market-scoped dataset."""
    return MARKET_DATASETS.register(ds, opt_in=opt_in, family=family)


def register_domain(
    ds: DomainDataset[Any], *, family: str | None = None
) -> DomainDataset[Any]:
    """Registers a sector / industry-scoped dataset."""
    return DOMAIN_DATASETS.register(ds, family=family)
