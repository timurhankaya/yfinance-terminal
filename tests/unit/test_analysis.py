"""Normalization of analyst datasets. No network, no database.

Hand-built frames are used instead of fixtures: each frame encodes an edge
case measured live, and the source of that measurement lives in the test
name. This lets the tests run without `scripts/capture_fixtures.py`, and
the code is the only place documenting the measurement.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import NormalizedResult
from yfin.datasets.payloads import AsOfFramePayload, AsOfMappingPayload, RangedFramePayload

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AS_OF = date(2026, 9, 4)


def _rows(result: NormalizedResult, table: str) -> list[dict[str, Any]]:
    return [row for write in result.writes if write.table == table for row in write.rows]


def _by(rows: list[dict[str, Any]], column: str) -> dict[Any, dict[str, Any]]:
    return {row[column]: row for row in rows}


# --- recommendations -------------------------------------------------------


def _recommendations_frame(periods: list[str]) -> pd.DataFrame:
    """`period` is a column, not the index; the source uses a RangeIndex."""
    return pd.DataFrame(
        {
            "period": periods,
            "strongBuy": [10] * len(periods),
            "buy": [9] * len(periods),
            "hold": [8] * len(periods),
            "sell": [1] * len(periods),
            "strongSell": [0] * len(periods),
        }
    )


def test_recommendations_reads_period_from_column_not_index() -> None:
    dataset = SYMBOL_DATASETS["recommendations"]
    payload = AsOfFramePayload(frame=_recommendations_frame(["0m", "-1m"]), fetched_at=NOW)

    rows = _rows(dataset.normalize(payload, "AAPL"), "analyst_recommendations")

    assert sorted(row["period"] for row in rows) == ["-1m", "0m"]
    assert rows[0]["strong_buy"] == 10
    assert rows[0]["as_of_date"] == AS_OF


def test_recommendations_accepts_three_row_frame() -> None:
    """10 of 19 symbols got 4 periods, 9 got 3; the count is not fixed."""
    dataset = SYMBOL_DATASETS["recommendations"]
    payload = AsOfFramePayload(frame=_recommendations_frame(["0m", "-1m", "-2m"]), fetched_at=NOW)

    assert len(_rows(dataset.normalize(payload, "THYAO.IS"), "analyst_recommendations")) == 3


def test_recommendations_drops_row_with_null_counter() -> None:
    """All five counters are NOT NULL: an incomplete row is not written.

    Writing it would trigger a NOT NULL violation, and since each symbol is
    one transaction, that would drop the ENTIRE symbol.
    """
    dataset = SYMBOL_DATASETS["recommendations"]
    frame = _recommendations_frame(["0m", "-1m"])
    frame.loc[1, "hold"] = None
    payload = AsOfFramePayload(frame=frame, fetched_at=NOW)

    rows = _rows(dataset.normalize(payload, "AAPL"), "analyst_recommendations")
    assert [row["period"] for row in rows] == ["0m"]


# --- estimates -------------------------------------------------------------


def _estimate_frame(year_ago_source: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "numberOfAnalysts": [1.0, float("nan")],
            "avg": [1.97656, 0],
            "low": [1.9, 0],
            "high": [2.0, 0],
            year_ago_source: [1.5, 1285436390920],
            "growth": [-0.05, None],
            "currency": ["USD", "USD"],
        },
        index=pd.Index(["0q", "+1q"], name="period"),
    )


def test_earnings_and_revenue_estimates_share_one_table_via_metric() -> None:
    earnings = SYMBOL_DATASETS["earnings_estimate"]
    revenue = SYMBOL_DATASETS["revenue_estimate"]

    eps_rows = _rows(
        earnings.normalize(AsOfFramePayload(_estimate_frame("yearAgoEps"), NOW), "AAPL"),
        "analyst_estimates",
    )
    rev_rows = _rows(
        revenue.normalize(AsOfFramePayload(_estimate_frame("yearAgoRevenue"), NOW), "AAPL"),
        "analyst_estimates",
    )

    assert {row["metric"] for row in eps_rows} == {"eps"}
    assert {row["metric"] for row in rev_rows} == {"revenue"}
    # Without metric in the PK, the two datasets would overwrite each other's row
    assert earnings.key_columns == ("symbol", "as_of_date", "metric", "period")


def test_estimate_year_ago_source_differs_per_metric() -> None:
    """`yearAgoEps` / `yearAgoRevenue` -- one column, two source keys."""
    revenue = SYMBOL_DATASETS["revenue_estimate"]
    rows = _by(
        _rows(
            revenue.normalize(AsOfFramePayload(_estimate_frame("yearAgoRevenue"), NOW), "AAPL"),
            "analyst_estimates",
        ),
        "period",
    )
    assert rows["+1q"]["year_ago_value"] == Decimal("1285436390920.0000000000")


def test_estimate_zero_is_not_null() -> None:
    """Measured revenue avg = 0 for THYAO's 0q/+1q periods; not NULL."""
    revenue = SYMBOL_DATASETS["revenue_estimate"]
    rows = _by(
        _rows(
            revenue.normalize(AsOfFramePayload(_estimate_frame("yearAgoRevenue"), NOW), "THYAO.IS"),
            "analyst_estimates",
        ),
        "period",
    )
    assert rows["+1q"]["avg"] == Decimal("0E-10")
    assert rows["+1q"]["avg"] is not None


def test_estimate_number_of_analysts_accepts_float_and_nan() -> None:
    earnings = SYMBOL_DATASETS["earnings_estimate"]
    rows = _by(
        _rows(
            earnings.normalize(AsOfFramePayload(_estimate_frame("yearAgoEps"), NOW), "AAPL"),
            "analyst_estimates",
        ),
        "period",
    )
    assert rows["0q"]["number_of_analysts"] == 1
    assert rows["+1q"]["number_of_analysts"] is None


def test_estimate_keeps_all_null_period_row() -> None:
    """Deliberate difference from `financial_facts`: an all-NULL period row is
    still written -- the period set is a fixed four, and NULL means "period
    exists, no estimate"."""
    earnings = SYMBOL_DATASETS["earnings_estimate"]
    frame = pd.DataFrame(
        {"avg": [None], "low": [None], "high": [None]},
        index=pd.Index(["0q"], name="period"),
    )
    rows = _rows(
        earnings.normalize(AsOfFramePayload(frame, NOW), "THYAO.IS"), "analyst_estimates"
    )
    assert len(rows) == 1
    assert rows[0]["avg"] is None


def test_estimate_uses_decimal_38_10_for_both_magnitudes() -> None:
    """The same column holds EPS 1.97656 and revenue 1_285_436_390_920."""
    earnings = SYMBOL_DATASETS["earnings_estimate"]
    rows = _by(
        _rows(
            earnings.normalize(AsOfFramePayload(_estimate_frame("yearAgoEps"), NOW), "AAPL"),
            "analyst_estimates",
        ),
        "period",
    )
    assert rows["0q"]["avg"] == Decimal("1.9765600000")


# --- eps_trend / eps_revisions ---------------------------------------------


def test_eps_trend_maps_numeric_prefixed_columns() -> None:
    """A column name cannot start with a digit: 7daysAgo -> days_ago_7."""
    dataset = SYMBOL_DATASETS["eps_trend"]
    frame = pd.DataFrame(
        {
            "current": [1.5],
            "7daysAgo": [1.4],
            "30daysAgo": [1.3],
            "60daysAgo": [1.2],
            "90daysAgo": [1.1],
            "currency": ["USD"],
        },
        index=pd.Index(["0q"], name="period"),
    )
    row = _rows(dataset.normalize(AsOfFramePayload(frame, NOW), "AAPL"), "analyst_eps_trend")[0]
    assert row["days_ago_7"] == Decimal("1.4000000000")
    assert row["days_ago_90"] == Decimal("1.1000000000")
    assert row["currency"] == "USD"


def test_eps_revisions_reads_capital_d_in_downlast7days() -> None:
    """`downLast7Days` capitalizes the D (measured on 19 of 19 symbols).

    Reading it with a lowercase `d` would leave the column silently NULL
    forever; the docs write all four in lowercase. That mismatch is exactly
    why this test exists.
    """
    dataset = SYMBOL_DATASETS["eps_revisions"]
    frame = pd.DataFrame(
        {
            "upLast7days": [3],
            "upLast30days": [5],
            "downLast7Days": [1],
            "downLast30days": [2],
            "currency": ["USD"],
        },
        index=pd.Index(["0q"], name="period"),
    )
    row = _rows(dataset.normalize(AsOfFramePayload(frame, NOW), "AAPL"), "analyst_eps_revisions")[
        0
    ]
    assert row["down_last_7d"] == 1
    assert row["down_last_30d"] == 2


def test_eps_revisions_lowercase_d_leaves_column_null() -> None:
    """If the source ever switches to lowercase `d`, the column stays NULL --
    the expected signal there is an `unmapped keys` warning, not a test
    failure."""
    dataset = SYMBOL_DATASETS["eps_revisions"]
    frame = pd.DataFrame({"downLast7days": [1]}, index=pd.Index(["0q"], name="period"))
    row = _rows(dataset.normalize(AsOfFramePayload(frame, NOW), "AAPL"), "analyst_eps_revisions")[
        0
    ]
    assert row["down_last_7d"] is None


# --- growth_estimates ------------------------------------------------------


def test_growth_estimates_accepts_ltg_period_and_missing_trends() -> None:
    """Index `0q,+1q,0y,+1y,LTG`; `industryTrend`/`sectorTrend` never showed
    up across 19 symbols, but their columns remain open."""
    dataset = SYMBOL_DATASETS["growth_estimates"]
    frame = pd.DataFrame(
        {"stockTrend": [0.1, 0.2], "indexTrend": [0.496, 0.496]},
        index=pd.Index(["0q", "LTG"], name="period"),
    )
    rows = _by(
        _rows(dataset.normalize(AsOfFramePayload(frame, NOW), "AAPL"), "analyst_growth_estimates"),
        "period",
    )
    assert set(rows) == {"0q", "LTG"}
    assert rows["LTG"]["index_trend"] == Decimal("0.496")
    assert rows["LTG"]["industry_trend"] is None


# --- analyst_price_targets -------------------------------------------------


def test_price_targets_keeps_zero_and_allows_low_above_current() -> None:
    """Measured low(330) > current(294) for THYAO; there is no consistency
    constraint. `currentPriceTarget = 0.0` is also a real value."""
    dataset = SYMBOL_DATASETS["analyst_price_targets"]
    payload = AsOfMappingPayload(
        payload={"current": 294, "low": 330, "high": 400, "mean": 0.0, "median": 350},
        fetched_at=NOW,
    )
    row = _rows(dataset.normalize(payload, "THYAO.IS"), "analyst_price_targets")[0]
    assert row["low"] > row["current"]
    assert row["mean"] == Decimal("0")
    assert row["mean"] is not None


def test_price_targets_empty_dict_is_empty_result() -> None:
    """SPY/VFIAX/BTC-USD return an empty dict; that is `empty`, not `failed`."""
    dataset = SYMBOL_DATASETS["analyst_price_targets"]
    assert dataset.normalize(AsOfMappingPayload({}, NOW), "SPY").is_empty


# --- upgrades_downgrades ---------------------------------------------------


def _grade_frame() -> pd.DataFrame:
    index = pd.Index(
        [pd.Timestamp("2024-09-30"), pd.Timestamp("2018-03-14")], dtype=object, name="GradeDate"
    )
    return pd.DataFrame(
        {
            "Firm": ["Morgan Stanley", "Goldman"],
            "ToGrade": ["Buy", ""],
            "FromGrade": ["", "Hold"],
            "Action": ["up", "main"],
            "priceTargetAction": ["Raises", ""],
            "currentPriceTarget": [250.0, 0.0],
            "priorPriceTarget": [200.0, None],
        },
        index=index,
    )


def test_grade_changes_maps_seven_columns_and_blanks_to_null() -> None:
    """Docs list four columns; measurement found seven. `''` -> NULL."""
    dataset = SYMBOL_DATASETS["upgrades_downgrades"]
    rows = _by(
        _rows(
            dataset.normalize(RangedFramePayload(_grade_frame(), NOW), "AAPL"),
            "analyst_grade_changes",
        ),
        "firm",
    )
    assert rows["Morgan Stanley"]["price_target_action"] == "Raises"
    assert rows["Morgan Stanley"]["from_grade"] is None
    assert rows["Goldman"]["to_grade"] is None
    # 0.0 is a real value ("no target" is different)
    assert rows["Goldman"]["current_price_target"] == Decimal("0")


def test_grade_changes_drops_row_with_empty_firm() -> None:
    """`firm` is part of the PK: an empty string would merge two distinct
    records into one row."""
    dataset = SYMBOL_DATASETS["upgrades_downgrades"]
    frame = _grade_frame()
    frame.loc[frame.index[0], "Firm"] = ""
    rows = _rows(
        dataset.normalize(RangedFramePayload(frame, NOW), "AAPL"), "analyst_grade_changes"
    )
    assert [row["firm"] for row in rows] == ["Goldman"]


def test_grade_changes_filters_by_range() -> None:
    dataset = SYMBOL_DATASETS["upgrades_downgrades"]
    payload = RangedFramePayload(
        _grade_frame(), NOW, start=date(2020, 1, 1), end=date(2025, 1, 1)
    )
    rows = _rows(dataset.normalize(payload, "AAPL"), "analyst_grade_changes")
    assert [row["firm"] for row in rows] == ["Morgan Stanley"]


def test_grade_changes_without_range_keeps_all_history() -> None:
    """Running without a filter writes the entire history Yahoo returns."""
    dataset = SYMBOL_DATASETS["upgrades_downgrades"]
    rows = _rows(
        dataset.normalize(RangedFramePayload(_grade_frame(), NOW), "AAPL"),
        "analyst_grade_changes",
    )
    assert len(rows) == 2


def test_grade_changes_timestamp_is_utc_without_second_conversion() -> None:
    """Source is tz-naive but derived from epochGradeDate in seconds -> UTC."""
    dataset = SYMBOL_DATASETS["upgrades_downgrades"]
    rows = _rows(
        dataset.normalize(RangedFramePayload(_grade_frame(), NOW), "AAPL"),
        "analyst_grade_changes",
    )
    stamps = {row["grade_ts_utc"] for row in rows}
    assert datetime(2024, 9, 30, 0, 0, tzinfo=UTC) in stamps


# --- earnings_history ------------------------------------------------------


def _earnings_history_frame() -> pd.DataFrame:
    index = pd.Index(
        [pd.Timestamp("2026-07-31"), pd.Timestamp("2025-10-31")], dtype=object, name="quarter"
    )
    return pd.DataFrame(
        {
            "epsActual": [1.2, 1.0],
            "epsEstimate": [1.1, 1.05],
            "epsDifference": [0.1, -0.05],
            "surprisePercent": [0.09, -0.047],
        },
        index=index,
    )


def test_earnings_history_keeps_fiscal_quarter_without_tz_conversion() -> None:
    """`quarter_end` is a calendar label, not an instant; NVDA/WMT's fiscal
    calendar drifts (2025-10-31 ... 2026-07-31)."""
    dataset = SYMBOL_DATASETS["earnings_history"]
    rows = _by(
        _rows(
            dataset.normalize(RangedFramePayload(_earnings_history_frame(), NOW), "NVDA"),
            "earnings_history",
        ),
        "quarter_end",
    )
    assert set(rows) == {date(2026, 7, 31), date(2025, 10, 31)}
    assert rows[date(2026, 7, 31)]["surprise_percent"] == Decimal("0.09")


def test_earnings_history_filters_by_range() -> None:
    dataset = SYMBOL_DATASETS["earnings_history"]
    payload = RangedFramePayload(_earnings_history_frame(), NOW, start=date(2026, 1, 1))
    rows = _rows(dataset.normalize(payload, "NVDA"), "earnings_history")
    assert [row["quarter_end"] for row in rows] == [date(2026, 7, 31)]


# --- contract ---------------------------------------------------------------


def test_analysis_alias_excludes_sustainability() -> None:
    """`sustainability` is a watch dataset: it has no table and returned 404
    on 19 of 19 symbols, so it is excluded from the alias."""
    assert "sustainability" not in SYMBOL_DATASETS.aliases["analysis"]


def test_recommendations_summary_is_an_alias_not_a_record() -> None:
    """Source body is `return self.get_recommendations(...)` (base.py:220)."""
    assert "recommendations_summary" not in SYMBOL_DATASETS
    assert SYMBOL_DATASETS.aliases["recommendations_summary"] == ("recommendations",)


def test_filter_datasets_declare_date_range() -> None:
    for name in ("upgrades_downgrades", "earnings_history", "insider_transactions"):
        assert SYMBOL_DATASETS[name].date_range == "filter"
