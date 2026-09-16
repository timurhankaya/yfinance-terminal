"""Dataset registry and resolver.

One `Registry` class, three instances (symbol, market, domain); the only
difference between them is whether a bootstrap dataset exists.
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

    Declaring `api` here does not catch a misspelled declaration: the
    bases default it to `()`, so `apis = (...)` silently resolves to `()`.
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

    `bootstrap`, when given, is prepended to every resolution whether or
    not the user selected it.
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
        #: together. A group that is simply "every dataset of this kind"
        #: is NOT written here; see `register(group=...)`.
        self.explicit_aliases: dict[str, tuple[str, ...]] = dict(aliases or {})
        #: Alias groups, collected from the registration sites.
        self._groups: dict[str, list[str]] = {}
        self._items: dict[str, D] = {}
        # Datasets that never run UNLESS NAMED EXPLICITLY. The opposite of
        # `bootstrap`: that one gets ADDED to every resolution, these get
        # EXCLUDED from `all`.
        self._opt_in: set[str] = set()

    # --- registration -------------------------------------------------

    @property
    def aliases(self) -> dict[str, tuple[str, ...]]:
        """Every name that expands to several: explicit ones and groups.

        A group cannot shadow a dataset or an explicit alias -- that is
        checked at registration -- so merging them is unambiguous.
        """
        return {
            **{name: tuple(members) for name, members in self._groups.items()},
            **self.explicit_aliases,
        }

    def register(self, ds: D, *, opt_in: bool = False, group: str | None = None) -> D:
        """Both flags are declared at the registration site, not in a list here.

        `group` names a `--datasets` alias (unrelated to `DataFamily`). `opt_in`
        keeps the dataset out of the `all` expansion; it still runs when named.
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
        if group is not None:
            if group in self.explicit_aliases:
                raise ValueError(
                    f"{ds.name}: group {group!r} is already an explicit alias"
                )
            members = self._groups.setdefault(group, [])
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
                # An ordinary expansion, so `--datasets all,search` adds an
                # opt-in dataset to the usual set.
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

        Expands aliases, dedups in order, sorts by depends_on, rejects
        unknown names and cycles; bootstrap is always prepended.
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
        # (base.py).
        "recommendations_summary": ("recommendations",),
        # `sustainability` declares no group: it has no table.
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


# `Registrable` needs only `name` + `depends_on`, which `DomainDataset` carries.
DOMAIN_DATASETS: Registry[DomainDataset[Any]] = Registry(
    bootstrap="domain_taxonomy",
    aliases={
        "sector": ("sector_profile", "sector_rankings"),
        "industry": ("industry_profile", "industry_rankings"),
    },
)


def register(
    ds: Dataset[Any], *, opt_in: bool = False, group: str | None = None
) -> Dataset[Any]:
    """Registers a symbol-scoped dataset (used as a decorator in dataset modules).

    `opt_in=True` -> excluded from the `all` expansion; `group=` puts it in
    a `--datasets <group>` alias. See `Registry.register`.
    """
    return SYMBOL_DATASETS.register(ds, opt_in=opt_in, group=group)


def register_market(
    ds: GlobalDataset[Any], *, opt_in: bool = False, group: str | None = None
) -> GlobalDataset[Any]:
    """Registers a market-scoped dataset."""
    return MARKET_DATASETS.register(ds, opt_in=opt_in, group=group)


def register_domain(
    ds: DomainDataset[Any], *, group: str | None = None
) -> DomainDataset[Any]:
    """Registers a sector / industry-scoped dataset."""
    return DOMAIN_DATASETS.register(ds, group=group)
