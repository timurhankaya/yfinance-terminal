"""Seventeen datasets writing to actual PostgreSQL.

ENUM, DECIMAL overflow, NOT NULL, reserved-word and type errors only surface in
a real INSERT, and each symbol is one transaction, so any one drops the symbol.
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
from yfin.datasets.base import Dataset
from yfin.datasets.payloads import (
    AsOfFramePayload,
    AsOfMappingPayload,
    FundsPayload,
    RangedFramePayload,
)
from yfin.storage.contracts import WriteStats
from yfin.storage.persistence import PostgresRowWriter

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
    assert not result.is_empty, f"{dataset.name}: test payload produced an empty result"
    return dataset.upsert(PostgresRowWriter(session), result)


def _assert_complete(stats: WriteStats, dataset: Dataset[Any]) -> None:
    """Every table written must have verified rows == attempted rows."""
    assert stats.attempted, f"{dataset.name}: nothing was written to any table"
    for table, attempted in stats.attempted.items():
        assert stats.verified[table] == attempted, f"{dataset.name}/{table}"


def _period_frame(columns: dict[str, list[Any]], periods: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns, index=pd.Index(periods, name="period"))


# --- analyst ---------------------------------------------------------------


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
    """The `metric` ENUM and DECIMAL(38,10): 1.97656 and 1.28e12 in the same column."""
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
    """Without `metric` in the PK, the second dataset would overwrite the first's row."""
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
    """PK (symbol, grade_ts_utc, firm) is 296 bytes, including a non-ASCII firm name."""
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


# --- ownership --------------------------------------------------------------


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
    """The largest holder values seen must fit DECIMAL(38,0)."""
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
    """net_trans can go negative, so the column is signed."""
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
    """PFE's two identical rows are deduplicated; `D/I` is not truncated."""
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
    # The two identical rows collapse to one; rows_attempted counts after dedup
    assert stats.attempted["insider_transactions"] == 2


def test_insider_roster_writes_eleven_column_variant(db_session: Session, symbol: str) -> None:
    """NVDA variant: 11 columns, `positionSummary` populated, raw epoch date."""
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


# --- fund -------------------------------------------------------------------


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
            # Out-of-universe symbols: an FK here would roll back the fund's whole row set
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
    """BND: 0 sectors + 9 ratings, `top_holdings` empty -- that table stays
    `empty` but its siblings are still written."""
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
    """`holding_symbol` has no FK; an unknown symbol is written with `is_known=0`."""
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
