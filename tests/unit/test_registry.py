"""Registry cozumleme testleri (S6.2)."""

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
    """Kayitlar: 10 eski kullanici-gorunur + symbols + 11 yeni (S6.5).
    'actions' ve 'financials' kayit degil, alias."""
    assert len(REGISTRY) == len(set(REGISTRY))
    assert "financials" not in REGISTRY
    assert "actions" not in REGISTRY
    assert ALIASES["actions"] == ("dividends", "splits", "capital_gains")


def test_user_visible_includes_alias_excludes_bootstrap() -> None:
    names = user_visible_names()
    assert "actions" in names
    assert BOOTSTRAP not in names
    assert "financials" in names
    assert set(names) == (set(REGISTRY) - {BOOTSTRAP}) | set(ALIASES)


def test_bootstrap_always_first() -> None:
    for selection in (None, ["news"], ["info", "history"], ["actions"]):
        assert resolve(selection)[0].name == BOOTSTRAP


def test_all_resolves_every_dataset() -> None:
    """`all` = OPT-IN OLMAYAN her dataset (SQ K11).

    `opt_in` `bootstrap`in tersidir: biri her cozumlemeye eklenir, digeri
    `all`dan cikarilir. Ikisini de disladiktan sonra kalan kume,
    kayitlarin tamamina esittir.
    """
    opt_in = {n for n in REGISTRY if REGISTRY.is_opt_in(n)}
    assert opt_in, "opt-in dataset bekleniyordu (search/lookup)"
    assert {d.name for d in resolve(None)} == set(REGISTRY) - opt_in
    assert {d.name for d in resolve(["all"])} == set(REGISTRY) - opt_in
    # Bos liste de `all` ile AYNI daldir (registry.py); opt-in orada da
    # disaridadir -- aksi halde `--datasets ""` gizli bir arka kapi olurdu.
    assert {d.name for d in resolve([])} == set(REGISTRY) - opt_in


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
    from yfin.datasets.base import Dataset

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

    REGISTRY.register(A())
    REGISTRY.register(B())
    try:
        with pytest.raises(DependencyCycleError):
            resolve(["_cycle_a"])
    finally:
        REGISTRY.unregister("_cycle_a")
        REGISTRY.unregister("_cycle_b")


def test_produces_are_table_names() -> None:
    """produces HER ZAMAN tablo adidir (S6.1/1); isin dataset'i ('symbols',)
    bildirir, hangi kolona yazdigi update_columns'ta durur."""
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
    """produces bir class attribute olmalidir; alt tipte property'ye
    donusturmek tabanin sozlesmesini daraltir (LSP)."""
    for name in ("dividends", "splits", "capital_gains"):
        dataset = REGISTRY[name]
        assert type(type(dataset).produces) is tuple
        assert dataset.produces == (name,)


def test_symbol_scoped_tables_covers_every_fk_child() -> None:
    """yfin symbols purge listesi FK grafinden turetilir; tek istisna
    FK TASIYAMAYAN tablolardir.

    `_FK_LESS_SYMBOL_TABLES` su anda BOSTUR. Tek elemani `price_bars`
    idi: MySQL'de partition'li oldugu icin FK tasiyamiyordu (ERROR 1506)
    ve FK grafinde hic gorunmuyordu. TimescaleDB hypertable'i referencing
    taraf olabildigi icin artik FK TASIYOR (PG S7.2) ve turetme onu
    kendiliginden buluyor -- yani bu invaryantin KAPSAMI GENISLEDI.

    Test iki yonlu calisir ve ikinci yonu bu gecisi yakaladi: (a) FK'li
    her cocuk turetilmis listede olmali, (b) elle listedeki her tablo
    GERCEKTEN FK tasimiyor olmali -- biri FK'li bir tabloyu orada
    birakirsa purge onu IKI KEZ silmeye calisirdi.
    """
    from yfin.models import _FK_LESS_SYMBOL_TABLES, Base, symbol_scoped_tables

    derived = set(symbol_scoped_tables())
    fk_children = {
        table.name
        for table in Base.metadata.tables.values()
        if table.name != "symbols"
        and any(fk.column.table.name == "symbols" for fk in table.foreign_keys)
    }
    assert derived == fk_children | set(_FK_LESS_SYMBOL_TABLES)

    for name in _FK_LESS_SYMBOL_TABLES:
        table = Base.metadata.tables[name]
        assert "symbol" in table.c, f"{name}: sembol kolonu yok"
        assert not any(fk.column.table.name == "symbols" for fk in table.foreign_keys), (
            f"{name} FK TASIYOR; elle listeden cikarilmali, turetme onu zaten bulur"
        )
    assert "price_history" in derived
    assert "news_symbols" not in derived  # FK yok (S5.5)


def test_purge_order_respects_foreign_keys() -> None:
    """Silme sirasi cocuktan ebeveyne dogru olmalidir."""
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
    """bootstrap=None dali: zorunlu on dataset eklenmez (S6.1)."""
    from yfin.datasets import MARKET_DATASETS

    assert MARKET_DATASETS.bootstrap is None
    resolved = MARKET_DATASETS.resolve(None)
    opt_in = {n for n in MARKET_DATASETS if MARKET_DATASETS.is_opt_in(n)}
    assert {d.name for d in resolved} == set(MARKET_DATASETS) - opt_in
    assert MARKET_DATASETS.resolve([]) == resolved


def test_financials_alias_expands_in_order() -> None:
    """8 statement dataset'i tek alias'la secilir; ttm_balance_sheet YOKTUR
    (freq='trailing' bilancoda ValueError firlatir)."""
    names = [d.name for d in resolve(["financials"])]
    assert names[0] == BOOTSTRAP
    assert names[1:] == list(ALIASES["financials"])
    assert "ttm_balance_sheet" not in REGISTRY


def test_new_datasets_are_registered() -> None:
    for name in ("calendar", "earnings_dates", "sec_filings"):
        assert name in REGISTRY
        assert REGISTRY[name].depends_on == (BOOTSTRAP,)
