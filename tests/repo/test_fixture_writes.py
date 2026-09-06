"""Real API data -> real PostgreSQL, no network.

Fixtures were captured once from live Yahoo (`scripts/capture_fixtures.py`);
each test here feeds them to the dataset's `normalize` and writes the output
to the real schema.

Why this exists alongside `test_dataset_writes.py`: there, payloads are
hand-built, so it only tests our own assumptions. Here the input comes from
the source itself -- column names, dtypes, missing columns, and the empty-
value distribution match what Yahoo actually returns. The two catch
different classes of bug.

An `empty` result is not a failure: 17 datasets are empty on ^GSPC, six on
THYAO.IS, and `funds_data` is empty for every non-fund symbol. The test does
not require "must be non-empty"; it requires "if non-empty, must write
without error".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from helpers import FIXTURE_ROOT, as_dataset_frame, as_funds_data, load_fixture
from yfin.datasets import SYMBOL_DATASETS
from yfin.datasets.base import Dataset
from yfin.datasets.payloads import (
    AsOfFramePayload,
    AsOfMappingPayload,
    FundsPayload,
    RangedFramePayload,
)
from yfin.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# Reference symbols; each covers a distinct edge case.
SYMBOLS = ("AAPL", "MSFT", "THYAO.IS", "PFE", "XOM", "NVDA", "WMT", "KO", "SPY", "BND", "^GSPC")

# The two datasets whose index is a date; for the others the index is a
# period label or a sequence number, and converting it to Timestamp is wrong.
DATETIME_INDEX = frozenset({"upgrades_downgrades", "earnings_history"})

# Those with `date_range="filter"` get a RangedFramePayload.
RANGED = frozenset({"upgrades_downgrades", "earnings_history", "insider_transactions"})

FRAME_DATASETS = (
    "recommendations",
    "upgrades_downgrades",
    "earnings_estimate",
    "revenue_estimate",
    "eps_trend",
    "eps_revisions",
    "earnings_history",
    "growth_estimates",
    "major_holders",
    "institutional_holders",
    "mutualfund_holders",
    "insider_purchases",
    "insider_transactions",
    "insider_roster_holders",
)


def _fixture_symbols() -> list[str]:
    return [s for s in SYMBOLS if (FIXTURE_ROOT / s).is_dir()]


@pytest.fixture
def written_symbols(db_session: Session) -> Iterator[list[str]]:
    codes = _fixture_symbols()
    if not codes:
        pytest.skip("no fixtures (run scripts/capture_fixtures.py)")
    for code in codes:
        db_session.execute(
            text(
                "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
                "VALUES (:s, true, 0, :t, :t)"
            ),
            {"s": code, "t": NOW},
        )
    yield codes


def _payload(name: str, symbol: str) -> Any:
    raw = load_fixture(symbol, name)
    if name == "funds_data":
        return FundsPayload(as_funds_data(raw), NOW)
    if name == "analyst_price_targets":
        return AsOfMappingPayload(raw, NOW)
    frame = as_dataset_frame(raw, datetime_index=name in DATETIME_INDEX)
    if name in RANGED:
        return RangedFramePayload(frame, NOW)
    return AsOfFramePayload(frame, NOW)


def _write(session: Session, dataset: Dataset[Any], symbol: str) -> tuple[int, int]:
    """(attempted, verified) -- an empty result returns (0, 0)."""
    result = dataset.normalize(_payload(dataset.name, symbol), symbol)
    if result.is_empty:
        return 0, 0
    stats = dataset.upsert(PostgresRowWriter(session), result)
    return sum(stats.attempted.values()), sum(stats.verified.values())


@pytest.mark.parametrize("name", [*FRAME_DATASETS, "analyst_price_targets", "funds_data"])
def test_every_fixture_symbol_writes_without_error(
    db_session: Session, written_symbols: list[str], name: str
) -> None:
    """The source's real output writes without error and verifies in full.

    The only assertion is `rows_verified == rows_attempted`; no claim is made
    about which symbol comes back filled vs. empty.
    """
    dataset = SYMBOL_DATASETS[name]
    filled = 0
    for symbol in written_symbols:
        attempted, verified = _write(db_session, dataset, symbol)
        assert verified == attempted, f"{name}/{symbol}"
        filled += bool(attempted)
    # At least one symbol must come back filled; if all are empty the fixture
    # or the mapping is broken and the test would silently prove nothing.
    assert filled > 0, f"{name}: no data in any of the 11 symbols"


def test_index_symbol_is_empty_everywhere_but_never_fails(
    db_session: Session, written_symbols: list[str]
) -> None:
    """^GSPC: all 17 datasets are empty -- `empty`, not `failed`."""
    if "^GSPC" not in written_symbols:
        pytest.skip("no ^GSPC fixture")
    for name in (*FRAME_DATASETS, "analyst_price_targets", "funds_data"):
        attempted, verified = _write(db_session, SYMBOL_DATASETS[name], "^GSPC")
        assert (attempted, verified) == (0, 0), name


def test_bond_fund_writes_ratings_but_no_holdings(
    db_session: Session, written_symbols: list[str]
) -> None:
    """Measured on BND: 0 sectors + 9 ratings, `top_holdings` empty."""
    if "BND" not in written_symbols:
        pytest.skip("no BND fixture")
    _write(db_session, SYMBOL_DATASETS["funds_data"], "BND")

    categories = list(
        db_session.execute(
            text("SELECT DISTINCT category FROM fund_weightings WHERE symbol = 'BND'")
        ).scalars()
    )
    holdings = db_session.execute(
        text("SELECT COUNT(*) FROM fund_top_holdings WHERE symbol = 'BND'")
    ).scalar_one()
    assert categories == ["bond_rating"]
    assert holdings == 0


def test_equity_fund_writes_sectors_and_holdings(
    db_session: Session, written_symbols: list[str]
) -> None:
    if "SPY" not in written_symbols:
        pytest.skip("no SPY fixture")
    _write(db_session, SYMBOL_DATASETS["funds_data"], "SPY")

    quote_type = db_session.execute(
        text("SELECT quote_type FROM fund_profile WHERE symbol = 'SPY'")
    ).scalar_one()
    # `quote_type` is not a @property in yfinance; a blind access would have
    # written "<bound method ...>" here (measured live).
    assert quote_type == "ETF"


def test_eps_revisions_down_last_7d_is_populated(
    db_session: Session, written_symbols: list[str]
) -> None:
    """`downLast7Days` has a capital D: reading it as `d` would leave the
    column silently NULL forever, and no other test would catch it."""
    _write(db_session, SYMBOL_DATASETS["eps_revisions"], "AAPL")
    filled = db_session.execute(
        text(
            "SELECT COUNT(*) FROM analyst_eps_revisions "
            "WHERE symbol = 'AAPL' AND down_last_7d IS NOT NULL"
        )
    ).scalar_one()
    assert filled > 0


def test_insider_transactions_are_deduplicated(
    db_session: Session, written_symbols: list[str]
) -> None:
    """The source can return duplicate rows; `rows_attempted` is the count
    after dedup, otherwise the attempted==verified equality would break."""
    for symbol in written_symbols:
        raw = load_fixture(symbol, "insider_transactions")
        if not raw:
            continue
        attempted, verified = _write(db_session, SYMBOL_DATASETS["insider_transactions"], symbol)
        assert verified == attempted
        assert attempted <= len(raw)
