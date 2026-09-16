"""Live integration test for analysis + holders + funds (`-m live`, not in CI).

Checks the per-symbol request count and the 404 rule (missing data is
`empty`, not `failed`), which only the live source can prove.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.datasets import SYMBOL_DATASETS
from yfin.ingest.client import configure_yfinance
from yfin.models import Base, ItemStatus, SyncRunItem
from yfin.pipeline.audit import EXIT_OK
from yfin.pipeline.runner import run_sync

pytestmark = pytest.mark.live

# Reference symbols; each is evidence for one edge case.
SYMBOLS = ("AAPL", "PFE", "XOM", "NVDA", "KO", "THYAO.IS", "SPY", "BND", "^GSPC")
DATASETS = ("analysis", "holders", "funds")

# Expected Yahoo module requests per symbol: recommendationTrend,
# upgradeDowngradeHistory, financialData, earningsTrend, earningsHistory,
# industry+sector+indexTrend, holders bundle.
EXPECTED_REQUESTS = 7


@pytest.fixture(scope="module", autouse=True)
def configured() -> None:
    """`run_sync` alone doesn't call `configure_yfinance`; without it yfinance
    hides exceptions and "no cell is failed" would approve data loss.
    """
    configure_yfinance(None, proxy_key="direct")


@pytest.fixture(scope="module")
def live_analysis(test_engine: Engine):  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        for symbol in SYMBOLS:
            session.execute(
                text(
                    "INSERT INTO symbols (symbol, is_active, unknown_streak, "
                    "created_at, updated_at) VALUES (:s, true, 0, now(), now()) "
                    "ON CONFLICT (symbol) DO NOTHING"
                ),
                {"s": symbol},
            )
        session.commit()
    datasets = SYMBOL_DATASETS.resolve(list(DATASETS))
    return run_sync(test_engine, list(SYMBOLS), datasets)


def _count(session: Session, table: str, symbol: str | None = None) -> int:
    target = Base.metadata.tables[table]
    stmt = select(func.count()).select_from(target)
    if symbol is not None:
        stmt = stmt.where(target.c["symbol"] == symbol)
    return int(session.execute(stmt).scalar_one())


def _statuses(session: Session, run_id: int, symbol: str) -> set[str]:
    rows = session.execute(
        select(SyncRunItem.status)
        .where(SyncRunItem.run_id == run_id)
        .where(SyncRunItem.symbol == symbol)
    ).scalars()
    return {ItemStatus(row).value for row in rows}


# --- run integrity ----------------------------------------------------------


def test_run_succeeds_despite_empty_symbols(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    """Empty cells for ^GSPC and BND do not make the run `partial`."""
    assert live_analysis.exit_code() == EXIT_OK
    assert live_analysis.failed == 0


def test_index_symbol_produces_only_empty_cells(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    """404 rule: missing data is `empty`, not `failed`."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        statuses = _statuses(session, live_analysis.run_id, "^GSPC")
    assert ItemStatus.FAILED.value not in statuses


def test_equity_symbol_fills_analyst_tables(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        assert _count(session, "analyst_recommendations", "AAPL") > 0
        assert _count(session, "analyst_estimates", "AAPL") > 0
        assert _count(session, "analyst_grade_changes", "AAPL") > 0
        assert _count(session, "holder_breakdown", "AAPL") > 0
        assert _count(session, "asof_state", "AAPL") > 0


def test_fund_symbol_fills_fund_tables(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        assert _count(session, "fund_profile", "SPY") == 1
        assert _count(session, "fund_top_holdings", "SPY") > 0
        # BND is a bond fund: profile is filled, top_holdings stays empty.
        assert _count(session, "fund_profile", "BND") == 1
        assert _count(session, "fund_weightings", "BND") > 0


def test_asof_row_count_matches_written_rows(live_analysis, test_engine: Engine) -> None:  # type: ignore[no-untyped-def]
    """`asof_state.row_count` is for auditing; must match rows actually written."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        written = _count(session, "analyst_recommendations", "AAPL")
        recorded = session.execute(
            text(
                "SELECT row_count FROM asof_state "
                "WHERE symbol = 'AAPL' AND dataset = 'recommendations'"
            )
        ).scalar_one()
    assert int(recorded) == written


# --- request count -----------------------------------------------------------


def test_symbol_costs_seven_requests(test_engine: Engine) -> None:
    """One `Ticker` per symbol; 16 datasets share 7 requests.

    If `news`'s fresh-Ticker exception is ever extended here, the cost rises
    to 16 requests and this test breaks -- that's exactly the point.
    """
    from yfinance.data import YfData

    from yfin.datasets.base import SyncContext
    from yfin.ingest.client import make_ticker

    calls: list[str] = []
    original = YfData.get_raw_json

    def counted(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(url)
        return original(self, url, *args, **kwargs)

    YfData.get_raw_json = counted  # type: ignore[method-assign]
    try:
        from datetime import UTC, datetime

        ctx = SyncContext("AAPL", make_ticker("AAPL"), datetime.now(UTC))
        for name in (*SYMBOL_DATASETS.aliases["analysis"], *SYMBOL_DATASETS.aliases["holders"]):
            SYMBOL_DATASETS[name].fetch(ctx)
    finally:
        YfData.get_raw_json = original  # type: ignore[method-assign]

    assert len(calls) == EXPECTED_REQUESTS, calls
