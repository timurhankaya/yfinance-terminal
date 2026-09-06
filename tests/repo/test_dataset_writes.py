"""ONYEDI dataset'in GERCEK MySQL'e yazimi (AH S8.5).

Unit testler `normalize`in URETTIGI satiri dogrular; burada o satirin
MySQL'e GERCEKTEN yazilabildigi dogrulanir. Ikisi ayri sinif hata yakalar:
yanlis ENUM degeri, tasan DECIMAL, NOT NULL ihlali, ayrilmis sozcuk ve
tip uyusmazligi yalnizca gercek bir INSERT'te gorunur -- ve sembol basina
tek transaction geregi bunlardan biri SEMBOLUN TAMAMINI dusururdu (S8.7).

Her dataset icin tek iddia: `rows_verified == rows_attempted` (S8.6).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import Dataset, WriteStats
from yfin.datasets.payloads import (
    AsOfFramePayload,
    AsOfMappingPayload,
    FundsPayload,
    RangedFramePayload,
)
from yfin.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

SYMBOL = "ZZWRITE"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)


@pytest.fixture
def symbol(db_session: Session) -> Iterator[str]:
    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, :t, :t)"
        ),
        {"s": SYMBOL, "t": NOW},
    )
    yield SYMBOL


def _write(session: Session, dataset: Dataset[Any], payload: Any) -> WriteStats:
    result = dataset.normalize(payload, SYMBOL)
    assert not result.is_empty, f"{dataset.name}: test payload'i bos sonuc uretti"
    return dataset.upsert(PostgresRowWriter(session), result)


def _assert_complete(stats: WriteStats, dataset: Dataset[Any]) -> None:
    """S8.6: yazilan her tabloda dogrulanmis satir = denenen satir."""
    assert stats.attempted, f"{dataset.name}: hicbir tabloya yazilmadi"
    for table, attempted in stats.attempted.items():
        assert stats.verified[table] == attempted, f"{dataset.name}/{table}"


def _period_frame(columns: dict[str, list[Any]], periods: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns, index=pd.Index(periods, name="period"))


# --- analist ---------------------------------------------------------------


def test_recommendations_writes(db_session: Session, symbol: str) -> None:
    frame = pd.DataFrame(
        {
            "period": ["0m", "-1m", "-2m"],
            "strongBuy": [12, 11, 10],
            "buy": [9, 9, 8],
            "hold": [8, 7, 7],
            "sell": [1, 1, 2],
            "strongSell": [0, 0, 1],
        }
    )
    dataset = SYMBOL_DATASETS["recommendations"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


@pytest.mark.parametrize(
    ("name", "year_ago"),
    [("earnings_estimate", "yearAgoEps"), ("revenue_estimate", "yearAgoRevenue")],
)
def test_estimates_write_with_enum_and_extreme_magnitudes(
    db_session: Session, symbol: str, name: str, year_ago: str
) -> None:
    """`metric` ENUM'u ve DECIMAL(38,10): AYNI kolonda 1.97656 ve 1.28e12."""
    frame = _period_frame(
        {
            "numberOfAnalysts": [30.0, float("nan")],
            "avg": [1.97656, 1_285_436_390_920],
            "low": [1.9, 0],
            "high": [2.1, 2_000_000_000_000],
            year_ago: [1.5, 1_000_000_000_000],
            "growth": [-0.0512, None],
            "currency": ["USD", "TRY"],
        },
        ["0q", "+1y"],
    )
    dataset = SYMBOL_DATASETS[name]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_both_estimate_datasets_coexist_in_one_table(db_session: Session, symbol: str) -> None:
    """`metric` PK'da olmasaydi ikinci dataset birincinin satirini EZERDI."""
    specs = (("earnings_estimate", "yearAgoEps"), ("revenue_estimate", "yearAgoRevenue"))
    for name, year_ago in specs:
        frame = _period_frame(
            {"avg": [1.0], "low": [0.5], "high": [1.5], year_ago: [0.9], "currency": ["USD"]},
            ["0q"],
        )
        _write(db_session, SYMBOL_DATASETS[name], AsOfFramePayload(frame, NOW))

    metrics = list(
        db_session.execute(
            text("SELECT metric FROM analyst_estimates WHERE symbol = :s ORDER BY metric"),
            {"s": symbol},
        ).scalars()
    )
    assert metrics == ["eps", "revenue"]


def test_eps_trend_writes(db_session: Session, symbol: str) -> None:
    frame = _period_frame(
        {
            "current": [1.5],
            "7daysAgo": [1.4],
            "30daysAgo": [1.3],
            "60daysAgo": [1.2],
            "90daysAgo": [1.1],
            "currency": ["USD"],
        },
        ["0q"],
    )
    dataset = SYMBOL_DATASETS["eps_trend"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_eps_revisions_writes(db_session: Session, symbol: str) -> None:
    frame = _period_frame(
        {
            "upLast7days": [3],
            "upLast30days": [5],
            "downLast7Days": [1],
            "downLast30days": [2],
            "currency": ["USD"],
        },
        ["0q"],
    )
    dataset = SYMBOL_DATASETS["eps_revisions"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_growth_estimates_writes_with_ltg_period(db_session: Session, symbol: str) -> None:
    frame = _period_frame(
        {"stockTrend": [0.1, 0.2], "indexTrend": [0.496, 0.1522]}, ["0q", "LTG"]
    )
    dataset = SYMBOL_DATASETS["growth_estimates"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_price_targets_write(db_session: Session, symbol: str) -> None:
    payload = AsOfMappingPayload(
        {"current": 294.0, "low": 330.0, "high": 400.0, "mean": 0.0, "median": 350.0}, NOW
    )
    dataset = SYMBOL_DATASETS["analyst_price_targets"]
    _assert_complete(_write(db_session, dataset, payload), dataset)


def test_grade_changes_write(db_session: Session, symbol: str) -> None:
    """PK (symbol, grade_ts_utc, firm) 296 byte; utf8mb4 firm adi dahil."""
    frame = pd.DataFrame(
        {
            "Firm": ["Morgan Stanley", "Türkiye İş Bankası"],
            "ToGrade": ["Overweight", ""],
            "FromGrade": ["", "Equal-Weight"],
            "Action": ["up", "main"],
            "priceTargetAction": ["Raises", ""],
            "currentPriceTarget": [250.0, 0.0],
            "priorPriceTarget": [200.0, None],
        },
        index=pd.Index(
            [pd.Timestamp("2024-09-30 13:45:00"), pd.Timestamp("2018-03-14")], dtype=object
        ),
    )
    dataset = SYMBOL_DATASETS["upgrades_downgrades"]
    _assert_complete(_write(db_session, dataset, RangedFramePayload(frame, NOW)), dataset)


def test_earnings_history_writes(db_session: Session, symbol: str) -> None:
    frame = pd.DataFrame(
        {
            "epsActual": [1.2, 1.0],
            "epsEstimate": [1.1, 1.05],
            "epsDifference": [0.1, -0.05],
            "surprisePercent": [0.09, -0.0476],
        },
        index=pd.Index([pd.Timestamp("2026-07-31"), pd.Timestamp("2025-10-31")], dtype=object),
    )
    dataset = SYMBOL_DATASETS["earnings_history"]
    _assert_complete(_write(db_session, dataset, RangedFramePayload(frame, NOW)), dataset)


# --- sahiplik --------------------------------------------------------------


def test_major_holders_writes(db_session: Session, symbol: str) -> None:
    frame = pd.DataFrame(
        {"Value": [0.0007, 0.6212, 0.6216, 7750.0]},
        index=pd.Index(
            [
                "insidersPercentHeld",
                "institutionsPercentHeld",
                "institutionsFloatPercentHeld",
                "institutionsCount",
            ]
        ),
    )
    dataset = SYMBOL_DATASETS["major_holders"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_institutional_holders_write_largest_measured_values(
    db_session: Session, symbol: str
) -> None:
    """Olculen max: shares 1.94e9, value 1.76e13 (JPM); DECIMAL(38,0)."""
    frame = pd.DataFrame(
        {
            "Date Reported": [pd.Timestamp("2026-06-30"), pd.NaT],
            "Holder": ["Vanguard Group Inc", "A" * 70],
            "pctHeld": [0.0876, 0.0654],
            "Shares": [1_940_000_000, 900_000_000],
            "Value": [17_600_000_000_000, 5_000_000_000],
            "pctChange": [0.0102, -0.02],
        }
    )
    dataset = SYMBOL_DATASETS["institutional_holders"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_insider_purchases_writes_negative_values(db_session: Session, symbol: str) -> None:
    """KO'da net -547_806 ve net_trans negatif olabilir -> SIGNED."""
    frame = pd.DataFrame(
        {
            "Insider Purchases Last 6m": [
                "Purchases",
                "Sales",
                "Net Shares Purchased (Sold)",
                "Total Insider Shares Held",
                "% Net Shares Purchased (Sold)",
                "% Buy Shares",
                "% Sell Shares",
            ],
            "Shares": [100_100, 647_906, -547_806, 3_000_000, -0.1542, 0.0231, 0.1773],
            "Trans": [1, 4, -3, None, None, None, None],
        }
    ).convert_dtypes()
    dataset = SYMBOL_DATASETS["insider_purchases"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


def test_insider_transactions_write(db_session: Session, symbol: str) -> None:
    """PFE'nin ozdes iki satiri tekillestirilir; `D/I` kirpilmaz."""
    frame = pd.DataFrame(
        {
            "Start Date": [pd.Timestamp("2025-02-21")] * 3,
            "Insider": ["BOSHOFF CHRISTOFFEL", "BOSHOFF CHRISTOFFEL", "Elliott Investment L.P"],
            "Position": ["Officer", "Officer", ""],
            "URL": ["", "", ""],
            "Transaction": ["", "", ""],
            "Text": ["Sale at price 26.00", "Sale at price 26.00", "Purchase"],
            "Shares": [8741, 8741, 1_000_000],
            "Value": [263716, 263716, float("nan")],
            "Ownership": ["D", "D", "D/I"],
        }
    )
    dataset = SYMBOL_DATASETS["insider_transactions"]
    stats = _write(db_session, dataset, RangedFramePayload(frame, NOW))
    _assert_complete(stats, dataset)
    # Ozdes iki satir TEKE iner; rows_attempted tekillestirme SONRASI sayidir
    assert stats.attempted["insider_transactions"] == 2


def test_insider_roster_writes_eleven_column_variant(db_session: Session, symbol: str) -> None:
    """NVDA varyanti: 11 kolon, `positionSummary` dolu, ham epoch tarih."""
    frame = pd.DataFrame(
        {
            "URL": [""],
            "Position": ["Beneficial Owner of more than 10% of a Class of Security"],
            "Name": ["Tim Cook"],
            "Most Recent Transaction": ["Sale"],
            "Latest Transaction Date": [pd.Timestamp("2026-04-01")],
            "Shares Owned Directly": [3_280_000],
            "Position Direct Date": [pd.Timestamp("2026-04-01")],
            "Position Indirect Date": [1_774_000_000.0],
            "Shares Owned Indirectly": [1_000],
            "positionSummary": [42_000],
            "positionSummaryDate": [1_774_000_000.0],
        }
    )
    dataset = SYMBOL_DATASETS["insider_roster_holders"]
    _assert_complete(_write(db_session, dataset, AsOfFramePayload(frame, NOW)), dataset)


# --- fon -------------------------------------------------------------------


def _funds_payload(*, sectors: dict[str, float], ratings: dict[str, float], holdings: int) -> Any:
    return {
        "quote_type": "ETF",
        "description": "x" * 555,
        "fund_overview": {"categoryName": "Large Blend", "family": "SPDR", "legalType": None},
        "fund_operations": pd.DataFrame(
            {"SPY": [0.0945, 0.02, 6.5e11], "Category Average": [0.5, 0.4, 2.0e10]},
            index=pd.Index(
                ["Annual Report Expense Ratio", "Annual Holdings Turnover", "Total Net Assets"]
            ),
        ),
        "asset_classes": {
            "cashPosition": 0.0123,
            "stockPosition": 0.9877,
            "bondPosition": 0.0,
            "preferredPosition": 0.0,
            "convertiblePosition": 0.0,
            "otherPosition": 0.0,
        },
        "top_holdings": pd.DataFrame(
            {
                "Name": [f"Holding {i}" for i in range(holdings)],
                "Holding Percent": [0.07 - i * 0.001 for i in range(holdings)],
            },
            # Evren disi semboller: FK olsaydi FONUN TUM VERISI rollback olurdu
            index=pd.Index(["005930.KQ", "2330.TW", "0700.HK", "VRTPX", "BRK-B"][:holdings]),
        ),
        "equity_holdings": pd.DataFrame(
            {
                "SPY": [25.0, 4.5, 3.1, 15.0, 1.0e11, 0.12],
                "Category Average": [24.0, 4.0, 3.0, 14.0, 9.0e10, 0.10],
            },
            index=pd.Index(
                [
                    "Price/Earnings",
                    "Price/Book",
                    "Price/Sales",
                    "Price/Cashflow",
                    "Median Market Cap",
                    "3 Year Earnings Growth",
                ]
            ),
        ),
        "bond_holdings": pd.DataFrame(
            {"SPY": [6.1, 8.4, None], "Category Average": [6.0, 8.0, None]},
            index=pd.Index(["Duration", "Maturity", "Credit Quality"]),
        ),
        "bond_ratings": ratings,
        "sector_weightings": sectors,
    }


def test_equity_fund_writes_all_four_tables(db_session: Session, symbol: str) -> None:
    payload = FundsPayload(
        _funds_payload(
            sectors={f"sector_{i}": 0.09 for i in range(11)},
            ratings={"aaa": 1.0},
            holdings=5,
        ),
        NOW,
    )
    dataset = SYMBOL_DATASETS["funds_data"]
    stats = _write(db_session, dataset, payload)
    _assert_complete(stats, dataset)
    assert set(stats.attempted) >= {
        "fund_profile",
        "fund_metrics",
        "fund_weightings",
        "fund_top_holdings",
        "asof_state",
    }


def test_bond_fund_writes_ratings_without_holdings(db_session: Session, symbol: str) -> None:
    """BND: 0 sektor + 9 rating, `top_holdings` BOS -- o tablo `empty` kalir
    ama kardesleri yazilir."""
    payload = FundsPayload(
        _funds_payload(
            sectors={},
            ratings={"us_government": 0.0, "aaa": 0.4012, "below_b": 0.0101},
            holdings=0,
        ),
        NOW,
    )
    dataset = SYMBOL_DATASETS["funds_data"]
    stats = _write(db_session, dataset, payload)
    _assert_complete(stats, dataset)
    assert stats.attempted["fund_top_holdings"] == 0
    assert stats.attempted["fund_weightings"] == 3


def test_out_of_universe_holdings_are_flagged_not_rejected(
    db_session: Session, symbol: str
) -> None:
    """`holding_symbol`de FK YOKTUR; bilinmeyen sembol `is_known=0` yazilir."""
    payload = FundsPayload(
        _funds_payload(sectors={"technology": 0.3}, ratings={"aaa": 1.0}, holdings=3), NOW
    )
    _write(db_session, SYMBOL_DATASETS["funds_data"], payload)

    rows = db_session.execute(
        text(
            "SELECT holding_symbol, is_known FROM fund_top_holdings "
            "WHERE symbol = :s ORDER BY holding_rank"
        ),
        {"s": symbol},
    ).all()
    assert [r.holding_symbol for r in rows] == ["005930.KQ", "2330.TW", "0700.HK"]
    assert all(r.is_known == 0 for r in rows)
