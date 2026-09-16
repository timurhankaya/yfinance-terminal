"""Registry resolution tests."""

from __future__ import annotations

import pytest

from yfin.datasets import (
    SYMBOL_DATASETS,
    DependencyCycleError,
    UnknownDatasetError,
)

REGISTRY = SYMBOL_DATASETS
BOOTSTRAP = SYMBOL_DATASETS.bootstrap
ALIASES = SYMBOL_DATASETS.aliases
resolve = SYMBOL_DATASETS.resolve
user_visible_names = SYMBOL_DATASETS.user_visible_names


def test_registry_record_count() -> None:
    """Records: 10 legacy user-visible + symbols + 11 new.
    'actions' and 'financials' are aliases, not records."""
    assert len(REGISTRY) == len(set(REGISTRY))
    assert "financials" not in REGISTRY
    assert "actions" not in REGISTRY
    assert ALIASES["actions"] == ("dividends", "splits", "capital_gains")


def test_user_visible_includes_alias_excludes_bootstrap() -> None:
    names = user_visible_names()
    assert "actions" in names
    assert BOOTSTRAP not in names
    assert "financials" in names
    # `all` is a name, so it appears in the unknown-name error's list.
    assert "all" in names
    assert set(names) == (set(REGISTRY) - {BOOTSTRAP}) | set(ALIASES) | {"all"}


def test_bootstrap_always_first() -> None:
    for selection in (None, ["news"], ["info", "history"], ["actions"]):
        assert resolve(selection)[0].name == BOOTSTRAP


def test_all_resolves_every_dataset() -> None:
    """`all` = every dataset that is NOT opt-in. `opt_in` is the inverse of `bootstrap`: one
    is added to every resolution, the other excluded from `all`."""
    default = {d.name for d in resolve(None)}
    opt_in = set(REGISTRY) - default
    assert {"search", "lookup"} <= opt_in
    for name in opt_in:
        assert name in {d.name for d in resolve([name])}
    assert {d.name for d in resolve(["all"])} == default
    # An empty list takes the same branch as `all` (registry.py); opt-in
    # stays excluded there too -- otherwise `--datasets ""` would be a
    # hidden backdoor.
    assert {d.name for d in resolve([])} == default


def test_alias_expands() -> None:
    assert [d.name for d in resolve(["actions"])] == [
        "symbols",
        "dividends",
        "splits",
        "capital_gains",
    ]


def test_order_preserving_dedup() -> None:
    names = [d.name for d in resolve(["news", "info", "news", "info"])]
    assert names == ["symbols", "news", "info"]


def test_unknown_name_rejected() -> None:
    with pytest.raises(UnknownDatasetError, match="bogus"):
        resolve(["bogus"])


def test_dependencies_come_first() -> None:
    for dataset in resolve(None):
        position = [d.name for d in resolve(None)].index(dataset.name)
        for dep in dataset.depends_on:
            assert [d.name for d in resolve(None)].index(dep) < position


def test_cycle_detected() -> None:
    from typing import Any

    from yfin.datasets.base import Dataset
    from yfin.datasets.registry import Registry

    class A(Dataset[None]):
        name = "_cycle_a"
        depends_on = ("_cycle_b",)

        def fetch(self, ctx):  # type: ignore[no-untyped-def]
            return None

        def normalize(self, raw, symbol):  # type: ignore[no-untyped-def]
            return None

    class B(A):
        name = "_cycle_b"
        depends_on = ("_cycle_a",)

    registry: Registry[Any] = Registry()
    registry.register(A())
    registry.register(B())
    with pytest.raises(DependencyCycleError):
        registry.resolve(["_cycle_a"])


def test_produces_are_table_names() -> None:
    """produces is always a table name; the isin dataset declares ('symbols',),
    and which column it writes lives in update_columns."""
    from yfin.models import Base

    for dataset in (REGISTRY[name] for name in REGISTRY):
        for table in dataset.produces:
            assert table in Base.metadata.tables, f"{dataset.name} -> {table}"
    assert REGISTRY["isin"].produces == ("symbols",)
    assert set(REGISTRY["info"].produces) == {
        "ticker_info",
        "ticker_info_history",
        "company_officers",
    }


def test_series_datasets_declare_produces_explicitly() -> None:
    """produces must be a class attribute; turning it into a property on a
    subtype would narrow the base contract (LSP)."""
    for name in ("dividends", "splits", "capital_gains"):
        dataset = REGISTRY[name]
        assert type(type(dataset).produces) is tuple
        assert dataset.produces == (name,)


def test_symbol_scoped_tables_covers_every_fk_child() -> None:
    """`yfin symbols purge`'s list is derived entirely from the FK graph and must equal the
    FK children exactly. A symbol-scoped table that cannot carry an FK would be silently
    skipped by purge; that blind spot is documented in `symbol_scoped_tables`."""
    from yfin.models import Base, symbol_scoped_tables

    derived = set(symbol_scoped_tables())
    fk_children = {
        table.name
        for table in Base.metadata.tables.values()
        if table.name != "symbols"
        and any(fk.column.table.name == "symbols" for fk in table.foreign_keys)
    }
    assert derived == fk_children
    assert "price_history" in derived
    assert "price_bars" in derived  # regained its FK via the hypertable
    assert "news_symbols" not in derived  # has no FK


def test_purge_order_respects_foreign_keys() -> None:
    """Deletion order must go from child to parent."""
    from yfin.models import Base, symbol_scoped_tables

    order = symbol_scoped_tables()
    position = {name: i for i, name in enumerate(order)}
    for name in order:
        table = Base.metadata.tables[name]
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent in position and parent != name:
                assert position[parent] > position[name], f"{name} -> {parent}"


def test_market_registry_has_no_bootstrap() -> None:
    """bootstrap=None branch: no mandatory prerequisite dataset is added."""
    from yfin.datasets import MARKET_DATASETS

    assert MARKET_DATASETS.bootstrap is None
    resolved = MARKET_DATASETS.resolve(None)
    assert set(MARKET_DATASETS) - {d.name for d in resolved} == {"screener"}
    assert MARKET_DATASETS.resolve([]) == resolved


def test_financials_alias_expands_in_order() -> None:
    """8 statement datasets are selected via one alias; ttm_balance_sheet
    does not exist (freq='trailing' raises ValueError on a balance sheet)."""
    names = [d.name for d in resolve(["financials"])]
    assert names[0] == BOOTSTRAP
    assert names[1:] == list(ALIASES["financials"])
    assert "ttm_balance_sheet" not in REGISTRY


def test_new_datasets_are_registered() -> None:
    for name in ("calendar", "earnings_dates", "sec_filings"):
        assert name in REGISTRY
        assert REGISTRY[name].depends_on == (BOOTSTRAP,)


def test_a_dataset_that_MISSPELLS_api_is_refused() -> None:
    """The registry reads `api` reflectively, so a misspelt attribute would be silently
    ignored. The protocol declares every field the registry reads, and an exposure naming a
    table the dataset does not produce is refused at registration."""
    from yfin.core.families import DataFamily
    from yfin.datasets.exposure import ApiExposure
    from yfin.datasets.registry import Registry

    class Misdeclared:
        name = "misdeclared"
        depends_on = ()
        produces = ("symbols",)
        api = (
            ApiExposure(
                family=DataFamily.REFERENCE,
                table="not_a_table_it_writes",
                sort_key=("symbol",),
            ),
        )

    registry: Registry[Misdeclared] = Registry()
    with pytest.raises(ValueError, match="not among the tables"):
        registry.register(Misdeclared())


def test_a_family_grows_with_its_MEMBERS() -> None:
    """`bars` derived its alias from the interval set and said why: a
    hand-written list that misses a new member registers it and then
    silently skips it. `financials`, `holders` and `analysis` were written
    by hand anyway, so a ninth statement would have been reachable by name,
    included in `all`, and quietly absent from `--datasets financials`."""
    from yfin.datasets.registry import Registry

    class _Member:
        depends_on = ()
        produces = ()
        api = ()

        def __init__(self, name: str) -> None:
            self.name = name

    registry: Registry[_Member] = Registry()
    registry.register(_Member("first"), group="things")
    assert registry.aliases["things"] == ("first",)

    registry.register(_Member("second"), group="things")
    assert registry.aliases["things"] == ("first", "second")
    assert [d.name for d in registry.resolve(["things"])] == ["first", "second"]


def test_a_family_cannot_shadow_an_explicit_alias() -> None:
    """Two names for one group, resolving differently depending on which
    won, is the ambiguity the merge has to be free of."""
    from yfin.datasets.registry import Registry

    class _Member:
        name = "member"
        depends_on = ()
        produces = ()
        api = ()

    registry: Registry[_Member] = Registry(aliases={"things": ("other",)})
    with pytest.raises(ValueError, match="already an explicit alias"):
        registry.register(_Member(), group="things")


def test_all_can_be_COMBINED_with_an_opt_in_dataset() -> None:
    """`--datasets all,search` is the natural way to add an opt-in dataset
    to the usual set."""
    names = [d.name for d in resolve(["all", "search"])]
    assert "search" in names
    assert "info" in names
    assert names.count("search") == 1
