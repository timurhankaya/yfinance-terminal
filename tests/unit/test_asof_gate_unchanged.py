"""`AsOfGate` ayristirmasinin SIFIR DAVRANIS DEGISIKLIGI kaniti (SI S9.2, S14/1).

Mixin `Dataset` hiyerarsisinden ayrildi ve `VOLATILE_COLUMNS`a
`first_seen_at` eklendi. Bu dosya iki seyi surer:

1. Mevcut 13 as-of dataset'inin kapi tablosu, anahtar kolonlari ve kapi
   kimligi BIREBIR eskisi gibidir.
2. `first_seen_at` eklemesi HICBIR mevcut hash'i degistiremez -- cunku
   sembol tarafindaki hicbir VERI tablosunda o kolon yoktur. Kanit sema
   uzerinden uretilir, elle yazilmis bir liste degildir.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.asof_base import (
    GATE_KEY_COLUMNS,
    GATE_TABLE,
    VOLATILE_COLUMNS,
    AsOfDataset,
    AsOfGate,
)
from yfin.datasets.base import Dataset, NormalizedResult, TableWrite
from yfin.models import Base

AS_OF = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# ALTIN DEGER: asagidaki deterministik govdenin SHA-256'si. Fixture'a
# BAGLI DEGILDIR (fixture'lar yeniden yakalandiginda kirilmasin diye);
# fenceledigi sey `content_hash`in ALGORITMASIDIR -- kolon eleme, tablo
# siralama ve satir siralama.
GOLDEN_HASH = "2a49b87018fd6c89edce127efc34f7c15df1baf6241c27eb479fd09b618076fe"


def _golden_result(order: tuple[str, ...]) -> NormalizedResult:
    return NormalizedResult(
        writes=[
            TableWrite(
                table="institutional_holders",
                rows=[
                    {
                        "symbol": "AAPL",
                        "as_of_date": AS_OF,
                        "holder_type": "institution",
                        "holder": holder,
                        "shares": Decimal("10"),
                        "fetched_at": NOW,
                    }
                    for holder in order
                ],
                key_columns=("symbol", "as_of_date", "holder_type", "holder"),
                update_columns=("shares", "fetched_at"),
            )
        ]
    )


def _asof_datasets() -> list[AsOfDataset[Any]]:
    """`asof_state` KAPI AILESI -- bu dosyanin konusu olan 13 dataset.

    Kapi tablosuna gore SUZULUR, yalnizca tipe gore DEGIL: SQ ile gelen
    `search`/`lookup` da `AsOfDataset`tir ama KENDI kapisini kullanir
    (`discovery_asof_state`, SQ K3a). Tipe gore suzmek bu dosyanin surdugu
    "13 dataset'in sozlesmesi degismedi" iddiasini, alakasiz bir ailenin
    buyumesiyle her seferinde kirardi.
    """
    return [
        SYMBOL_DATASETS[name]
        for name in SYMBOL_DATASETS
        if isinstance(SYMBOL_DATASETS[name], AsOfDataset)
        and SYMBOL_DATASETS[name].asof_gate_table == GATE_TABLE
    ]


def _discovery_datasets() -> list[AsOfDataset[Any]]:
    """`discovery_asof_state` kapi ailesi (SQ S6.2)."""
    from yfin.datasets.discovery.base import DISCOVERY_GATE_TABLE

    return [
        SYMBOL_DATASETS[name]
        for name in SYMBOL_DATASETS
        if isinstance(SYMBOL_DATASETS[name], AsOfDataset)
        and SYMBOL_DATASETS[name].asof_gate_table == DISCOVERY_GATE_TABLE
    ]


def test_there_are_still_thirteen_symbol_side_asof_datasets() -> None:
    assert len(_asof_datasets()) == 13


def test_symbol_side_gate_contract_is_unchanged() -> None:
    for dataset in _asof_datasets():
        assert dataset.asof_gate_table == GATE_TABLE == "asof_state"
        assert dataset.asof_gate_key_columns == GATE_KEY_COLUMNS == ("symbol", "dataset")
        assert dataset.produces[-1] == GATE_TABLE


def test_symbol_side_gate_identity_is_symbol_and_dataset() -> None:
    dataset = SYMBOL_DATASETS["institutional_holders"]
    result = NormalizedResult(
        writes=[
            TableWrite(
                table="institutional_holders",
                rows=[
                    {
                        "symbol": "AAPL",
                        "as_of_date": AS_OF,
                        "holder": "Vanguard",
                        "shares": Decimal("10"),
                        "fetched_at": NOW,
                    }
                ],
                key_columns=("symbol", "as_of_date", "holder_type", "holder"),
                update_columns=("shares",),
            )
        ]
    )
    assert dataset.gate_identity(result) == {
        "symbol": "AAPL",
        "dataset": "institutional_holders",
    }


def test_first_seen_at_cannot_affect_any_existing_hash() -> None:
    """`asof_state` ailesinin hicbir VERI tablosunda `first_seen_at` YOK.

    Kolonu tasiyan tek sembol-tarafi tablosu kapinin kendisidir
    (`asof_state`) ve o hash GOVDESINE hic girmez.
    """
    carriers = {
        table.name
        for table in Base.metadata.tables.values()
        if "first_seen_at" in table.c
    }
    symbol_side_targets = {
        table
        for dataset in _asof_datasets()
        for table in dataset.produces
        if table != GATE_TABLE
    }
    assert symbol_side_targets & carriers == set()
    assert "first_seen_at" in VOLATILE_COLUMNS
    assert GATE_TABLE in carriers


def test_discovery_family_uses_its_own_gate() -> None:
    """SQ K3a: `asof_state` KULLANILAMAZ.

    O tablonun `symbol` kolonu `symbols.symbol`a FK tasir; serbest arama
    terimi orada olmadigi icin kapi satiri `ERROR 1452` alirdi. Bu test,
    birinin "tutarlilik olsun" diye kesif dataset'lerini eski kapiya
    tasimasini engeller.
    """
    from yfin.datasets.discovery.base import (
        DISCOVERY_GATE_KEY_COLUMNS,
        DISCOVERY_GATE_TABLE,
    )

    family = _discovery_datasets()
    assert {d.name for d in family} == {"search", "lookup"}
    for dataset in family:
        assert dataset.asof_gate_table == DISCOVERY_GATE_TABLE
        assert dataset.asof_gate_key_columns == DISCOVERY_GATE_KEY_COLUMNS
        assert dataset.produces[-1] == DISCOVERY_GATE_TABLE


def test_discovery_first_seen_at_carrier_is_ungated() -> None:
    """`research_reports` `first_seen_at` TASIR ve `search`in `produces`
    listesindedir -- ama KAPI DISINDADIR (SQ S6.2.1).

    Yani hash govdesine hic girmez ve `first_seen_at`in "her kosuda degisir"
    ozelligi kapiyi bozamaz. Bu, ustteki testin kesif ailesindeki
    karsiligidir; kanit `UNGATED_TABLES`tan turetilir, elle yazilmis bir
    istisna listesinden degil.
    """
    from yfin.datasets.discovery.base import UNGATED_TABLES

    carriers = {
        table.name for table in Base.metadata.tables.values() if "first_seen_at" in table.c
    }
    gated_targets = {
        table
        for dataset in _discovery_datasets()
        for table in dataset.produces
        if table != dataset.asof_gate_table and table not in UNGATED_TABLES
    }
    assert gated_targets & carriers == set()
    # Iddianin BOS OLMADIGININ kaniti: tasiyici gercekten `produces`ta ve
    # gercekten kapi disinda.
    assert "research_reports" in carriers
    assert "research_reports" in SYMBOL_DATASETS["search"].produces
    assert "research_reports" in UNGATED_TABLES


def test_asof_dataset_is_still_a_dataset() -> None:
    """LSP: sembol tarafi runner'i `Dataset` bekliyor; mixin bunu bozmaz."""
    for dataset in _asof_datasets():
        assert isinstance(dataset, Dataset)
        assert isinstance(dataset, AsOfGate)


def test_domain_side_uses_a_different_gate_without_touching_the_symbol_side() -> None:
    from yfin.datasets.domain.base import DomainAsOfDataset
    from yfin.datasets.registry import DOMAIN_DATASETS

    for name in DOMAIN_DATASETS:
        dataset = DOMAIN_DATASETS[name]
        if not isinstance(dataset, DomainAsOfDataset):
            continue
        assert dataset.asof_gate_table == "domain_asof_state"
        # IKI HIYERARSI BIRLESTIRILEMEZ: domain dataset'i bir `Dataset`
        # DEGILDIR ve olmamalidir (fetch/normalize imzalari farkli).
        assert not isinstance(dataset, Dataset)
        assert isinstance(dataset, AsOfGate)


def test_content_hash_matches_the_golden_value() -> None:
    """Hash ALGORITMASI regresyon citi.

    `AsOfGate`in `content_hash` govdesi refaktorde tasindı ama
    DEGISTIRILMEDI; bu deger onu kalici olarak sabitler.
    """
    dataset = SYMBOL_DATASETS["institutional_holders"]
    assert dataset.content_hash(_golden_result(("Vanguard", "BlackRock"))) == GOLDEN_HASH


def test_row_order_does_not_change_the_golden_value() -> None:
    dataset = SYMBOL_DATASETS["institutional_holders"]
    assert dataset.content_hash(_golden_result(("BlackRock", "Vanguard"))) == GOLDEN_HASH
