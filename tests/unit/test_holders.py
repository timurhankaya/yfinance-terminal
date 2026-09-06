"""Sahiplik/insider dataset'lerinin normalizasyonu (AH S9.1). Agsiz, DB'siz."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import NormalizedResult, TableWrite
from yfin.datasets.payloads import AsOfFramePayload, RangedFramePayload

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)


def _rows(result: NormalizedResult, table: str) -> list[dict[str, Any]]:
    return [row for write in result.writes if write.table == table for row in write.rows]


def _write(result: NormalizedResult, table: str) -> TableWrite:
    return next(write for write in result.writes if write.table == table)


# --- major_holders ---------------------------------------------------------


def test_major_holders_maps_four_keys_from_index() -> None:
    """Kaynak sozlugu tek kolonlu ('Value') bir cerceveye ceviriyor; index
    ANAHTAR adlaridir. `institutionsCount` float geliyor (7750.0)."""
    dataset = SYMBOL_DATASETS["major_holders"]
    frame = pd.DataFrame(
        {"Value": [0.0007, 0.62, 0.62, 7750.0]},
        index=pd.Index(
            [
                "insidersPercentHeld",
                "institutionsPercentHeld",
                "institutionsFloatPercentHeld",
                "institutionsCount",
            ]
        ),
    )
    row = _rows(dataset.normalize(AsOfFramePayload(frame, NOW), "AAPL"), "holder_breakdown")[0]
    assert row["insiders_pct_held"] == Decimal("0.0007")
    assert row["institutions_count"] == 7750
    assert row["as_of_date"] == AS_OF


# --- institutional / mutualfund --------------------------------------------


def _holder_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date Reported": [pd.Timestamp("2026-06-30"), pd.Timestamp("2026-03-31")],
            "Holder": ["Vanguard Group Inc", "Blackrock Inc."],
            "pctHeld": [0.0876, 0.0654],
            "Shares": [1_940_000_000, 900_000_000],
            "Value": [17_600_000_000_000, 5_000_000_000],
            "pctChange": [0.01, -0.02],
        }
    )


def test_holder_datasets_share_one_table_and_split_by_holder_type() -> None:
    institutional = SYMBOL_DATASETS["institutional_holders"]
    mutualfund = SYMBOL_DATASETS["mutualfund_holders"]

    inst = institutional.normalize(AsOfFramePayload(_holder_frame(), NOW), "AAPL")
    fund = mutualfund.normalize(AsOfFramePayload(_holder_frame(), NOW), "AAPL")

    assert {row["holder_type"] for row in _rows(inst, "institutional_holders")} == {"institution"}
    assert {row["holder_type"] for row in _rows(fund, "institutional_holders")} == {"mutualfund"}
    # ENUM degeri DAIMA kucuk harf; DB'nin ai_ci sessiz donusumune guvenilmez
    assert all(row["holder_type"].islower() for row in _rows(inst, "institutional_holders"))


def test_holder_scope_values_are_explicit_so_deletion_survives_empty_source() -> None:
    """`scope_values` verilmeseydi `_delete_scope` kapsami SATIRLARDAN
    turetir ve kaynak bosaldiginda silme HIC yapilmazdi (AH S7.2)."""
    dataset = SYMBOL_DATASETS["institutional_holders"]
    write = _write(
        dataset.normalize(AsOfFramePayload(_holder_frame(), NOW), "AAPL"),
        "institutional_holders",
    )
    assert write.mode == "replace_scope"
    assert write.scope_columns == ("symbol", "as_of_date", "holder_type")
    assert write.scope_values == (
        {"symbol": "AAPL", "as_of_date": AS_OF, "holder_type": "institution"},
    )


def test_holder_date_reported_varies_per_row() -> None:
    """AAPL mutualfund'da TEK listede dort farkli tarih olculdu; tarih tablo
    basligina tasinamaz."""
    dataset = SYMBOL_DATASETS["mutualfund_holders"]
    rows = _rows(
        dataset.normalize(AsOfFramePayload(_holder_frame(), NOW), "AAPL"), "institutional_holders"
    )
    assert {row["date_reported"] for row in rows} == {date(2026, 6, 30), date(2026, 3, 31)}


def test_holder_large_values_survive_decimal_38_0() -> None:
    """Olculen max: shares 1.94e9, value 1.76e13 (JPM)."""
    dataset = SYMBOL_DATASETS["institutional_holders"]
    rows = _rows(
        dataset.normalize(AsOfFramePayload(_holder_frame(), NOW), "JPM"), "institutional_holders"
    )
    assert max(row["value"] for row in rows) == Decimal("17600000000000")


# --- insider_purchases -----------------------------------------------------


def _purchases_frame(period: str = "6m") -> pd.DataFrame:
    """0. kolonun ADI dinamiktir; satir etiketleri o kolonun DEGERLERIDIR."""
    return pd.DataFrame(
        {
            f"Insider Purchases Last {period}": [
                "Purchases",
                "Sales",
                "Net Shares Purchased (Sold)",
                "Total Insider Shares Held",
                "% Net Shares Purchased (Sold)",
                "% Buy Shares",
                "% Sell Shares",
            ],
            "Shares": [100, 647_906, -547_806, 3_000_000, -0.15, 0.02, 0.17],
            "Trans": [1, 4, -3, None, None, None, None],
        }
    )


def test_insider_purchases_pivots_seven_rows_into_one() -> None:
    dataset = SYMBOL_DATASETS["insider_purchases"]
    result = dataset.normalize(AsOfFramePayload(_purchases_frame(), NOW), "KO")
    rows = _rows(result, "insider_activity")
    assert len(rows) == 1
    row = rows[0]
    assert row["period_label"] == "6m"
    assert row["purchases_trans"] == 1
    assert row["buy_pct"] == Decimal("0.02")


def test_insider_purchases_allows_negative_net_shares() -> None:
    """KO'da net -547_806 olculdu -> isaretli DECIMAL(38,0) ve SIGNED INT."""
    dataset = SYMBOL_DATASETS["insider_purchases"]
    row = _rows(
        dataset.normalize(AsOfFramePayload(_purchases_frame(), NOW), "KO"), "insider_activity"
    )[0]
    assert row["net_shares"] == Decimal("-547806")
    assert row["net_trans"] == -3


def test_insider_purchases_reads_period_from_dynamic_header() -> None:
    dataset = SYMBOL_DATASETS["insider_purchases"]
    row = _rows(
        dataset.normalize(AsOfFramePayload(_purchases_frame("3m"), NOW), "AAPL"),
        "insider_activity",
    )[0]
    assert row["period_label"] == "3m"


def test_insider_purchases_without_matching_header_writes_nothing() -> None:
    """`period_label` NOT NULL: desen tutmazsa satir YAZILAMAZ."""
    dataset = SYMBOL_DATASETS["insider_purchases"]
    frame = _purchases_frame()
    frame.columns = ["Something Else", "Shares", "Trans"]
    assert dataset.normalize(AsOfFramePayload(frame, NOW), "AAPL").is_empty


# --- insider_transactions --------------------------------------------------


def _transactions_frame(rows: int = 2) -> pd.DataFrame:
    base = {
        "Start Date": [pd.Timestamp("2025-02-21")] * rows,
        "Insider": ["BOSHOFF CHRISTOFFEL"] * rows,
        "Position": ["Officer"] * rows,
        "URL": [""] * rows,
        "Transaction": [""] * rows,
        "Text": ["Sale at price 26.00"] * rows,
        "Shares": [8741] * rows,
        "Value": [263716] * rows,
        "Ownership": ["D"] * rows,
    }
    return pd.DataFrame(base)


def test_insider_transactions_deduplicates_identical_rows() -> None:
    """PFE'de DOKUZ KOLONUN TAMAMINDA ozdes iki satir olculdu ve `fact_hash`
    onlari AYIRAMAZ. Tekillestirme olmasaydi 2 satir okunup 1 yazilir,
    `rows_verified != rows_attempted` yanlis `failed` uretirdi (S8.5)."""
    dataset = SYMBOL_DATASETS["insider_transactions"]
    rows = _rows(
        dataset.normalize(RangedFramePayload(_transactions_frame(2), NOW), "PFE"),
        "insider_transactions",
    )
    assert len(rows) == 1


def test_insider_transactions_sentinels_become_null() -> None:
    """`Transaction` ve `URL` 16 sembol / 1464 satirin HEPSINDE ''."""
    dataset = SYMBOL_DATASETS["insider_transactions"]
    row = _rows(
        dataset.normalize(RangedFramePayload(_transactions_frame(1), NOW), "PFE"),
        "insider_transactions",
    )[0]
    assert row["transaction_label"] is None
    assert row["url"] is None


def test_insider_transactions_keeps_three_character_ownership() -> None:
    """XOM'da `D/I` olculdu; VARCHAR(2) bunu KIRPARDI."""
    dataset = SYMBOL_DATASETS["insider_transactions"]
    frame = _transactions_frame(1)
    frame.loc[0, "Ownership"] = "D/I"
    row = _rows(
        dataset.normalize(RangedFramePayload(frame, NOW), "XOM"), "insider_transactions"
    )[0]
    assert row["ownership"] == "D/I"


def test_insider_transactions_null_value_survives() -> None:
    """DIS ve BP.L'de `Value` TUM satirlarda NaN."""
    dataset = SYMBOL_DATASETS["insider_transactions"]
    frame = _transactions_frame(1)
    frame["Value"] = [float("nan")]
    row = _rows(
        dataset.normalize(RangedFramePayload(frame, NOW), "DIS"), "insider_transactions"
    )[0]
    assert row["value"] is None


def test_insider_transactions_filters_by_range() -> None:
    dataset = SYMBOL_DATASETS["insider_transactions"]
    payload = RangedFramePayload(_transactions_frame(1), NOW, start=date(2026, 1, 1))
    assert dataset.normalize(payload, "PFE").is_empty


# --- insider_roster --------------------------------------------------------


def _roster_frame(columns: int) -> pd.DataFrame:
    """Kaynak kolon seti sembole gore 7 / 9 / 11'dir, SIRASI DA SABIT DEGIL."""
    data: dict[str, Any] = {
        "URL": [""],
        "Position": ["Chief Executive Officer"],
        "Name": ["Tim Cook"],
        "Most Recent Transaction": ["Sale"],
        "Latest Transaction Date": [pd.Timestamp("2026-04-01")],
        "Shares Owned Directly": [3_280_000],
        "Position Direct Date": [pd.Timestamp("2026-04-01")],
    }
    if columns >= 9:
        # Ham epoch float64 gelebiliyor (6 sembolde dolu olculdu)
        data["Position Indirect Date"] = [1_774_000_000.0]
        data["Shares Owned Indirectly"] = [1_000]
    if columns >= 11:
        data["positionSummary"] = [42_000]
        data["positionSummaryDate"] = [1_774_000_000.0]
    return pd.DataFrame(data)


def test_insider_roster_handles_seven_nine_and_eleven_columns() -> None:
    dataset = SYMBOL_DATASETS["insider_roster_holders"]
    for count in (7, 9, 11):
        rows = _rows(
            dataset.normalize(AsOfFramePayload(_roster_frame(count), NOW), "NVDA"),
            "insider_roster",
        )
        assert rows[0]["name"] == "Tim Cook"
        assert rows[0]["position"] == "Chief Executive Officer"


def test_insider_roster_position_summary_is_typed_column() -> None:
    """NVDA'da YALNIZ bu kolon doluydu; alinmasaydi o satirin TUM hisse
    alanlari NULL kalirdi."""
    dataset = SYMBOL_DATASETS["insider_roster_holders"]
    row = _rows(
        dataset.normalize(AsOfFramePayload(_roster_frame(11), NOW), "NVDA"), "insider_roster"
    )[0]
    assert row["position_summary"] == Decimal("42000")
    assert row["position_summary_date"] is not None


def test_insider_roster_accepts_timestamp_and_raw_epoch_dates() -> None:
    dataset = SYMBOL_DATASETS["insider_roster_holders"]
    row = _rows(
        dataset.normalize(AsOfFramePayload(_roster_frame(9), NOW), "KO"), "insider_roster"
    )[0]
    assert row["position_direct_date"] == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    assert row["position_indirect_date"] is not None


def test_insider_roster_replaces_scope_with_explicit_values() -> None:
    dataset = SYMBOL_DATASETS["insider_roster_holders"]
    write = _write(
        dataset.normalize(AsOfFramePayload(_roster_frame(7), NOW), "AAPL"), "insider_roster"
    )
    assert write.mode == "replace_scope"
    assert write.scope_values == ({"symbol": "AAPL", "as_of_date": AS_OF},)


# --- sozlesme --------------------------------------------------------------


def test_holders_alias_covers_six_datasets() -> None:
    assert len(SYMBOL_DATASETS.aliases["holders"]) == 6


def test_asof_datasets_declare_gate_table_in_produces() -> None:
    """`produces` KAPI TABLOSUNU DA icerir; bildirilmeseydi `_failed_records`
    hata yolunda kapi satirini denetimden dusururdu (AH S6.1)."""
    for name in (
        "major_holders",
        "institutional_holders",
        "mutualfund_holders",
        "insider_purchases",
        "insider_roster_holders",
        "funds_data",
        "recommendations",
        "analyst_price_targets",
    ):
        assert "asof_state" in SYMBOL_DATASETS[name].produces, name


def test_pure_upsert_datasets_do_not_declare_gate_table() -> None:
    """Kaynagin kendi tarihini tasidigi uc dataset AS-OF DEGILDIR."""
    for name in ("upgrades_downgrades", "earnings_history", "insider_transactions"):
        assert "asof_state" not in SYMBOL_DATASETS[name].produces, name
