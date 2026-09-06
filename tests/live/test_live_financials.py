"""Live integration test for financials + market.

Skipped by default. Run manually with: pytest -m live
Uses the real Yahoo API and a real PostgreSQL; not run in CI.

`run_sync` takes the 'yfin_sync' advisory lock. If another sync is running,
this module fails with `LockNotAcquired` -- that means the concurrency
protection works, it is not a test bug.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.datasets import MARKET_DATASETS, SYMBOL_DATASETS
from yfin.market_runner import run_market_sync
from yfin.models import Base
from yfin.runner import EXIT_OK, run_sync

pytestmark = pytest.mark.live

SYMBOLS = ("AAPL", "MSFT", "THYAO.IS", "SPY")
FINANCIAL_DATASETS = ("financials", "valuation", "calendar", "earnings_dates", "sec_filings")


@pytest.fixture(scope="module")
def live_financials(test_engine: Engine):  # type: ignore[no-untyped-def]
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
    datasets = SYMBOL_DATASETS.resolve(list(FINANCIAL_DATASETS))
    return run_sync(test_engine, list(SYMBOLS), datasets)


def _count(session: Session, table: str, symbol: str | None = None) -> int:
    target = Base.metadata.tables[table]
    stmt = select(func.count()).select_from(target)
    if symbol is not None:
        stmt = stmt.where(target.c["symbol"] == symbol)
    return int(session.execute(stmt).scalar_one())


class TestLiveFinancials:
    def test_run_succeeds(self, live_financials) -> None:  # type: ignore[no-untyped-def]
        assert live_financials.failed == 0
        assert live_financials.exit_code() == EXIT_OK

    def test_companies_have_statements(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        with Session(test_engine) as session:
            for symbol in ("AAPL", "MSFT", "THYAO.IS"):
                assert _count(session, "financial_periods", symbol) > 0
                assert _count(session, "financial_facts", symbol) > 0

    def test_valuation_measures_land_in_the_same_eav(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        """No separate table: measures live in the same EAV with `statement='valuation'`."""
        with Session(test_engine) as session:
            rows = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM financial_facts "
                        "WHERE statement = 'valuation' AND symbol = 'AAPL'"
                    )
                ).scalar_one()
            )
            freqs = set(
                session.execute(
                    text(
                        "SELECT DISTINCT freq FROM financial_periods "
                        "WHERE statement = 'valuation'"
                    )
                ).scalars()
            )
        assert rows > 0
        assert freqs == {"annual", "quarterly"}

    def test_etf_has_no_valuation_measures(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        """The source returns (0,0) for SPY: `empty`, not `failed`."""
        with Session(test_engine) as session:
            rows = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM financial_periods "
                        "WHERE statement = 'valuation' AND symbol = 'SPY'"
                    )
                ).scalar_one()
            )
        assert rows == 0

    def test_etf_statements_are_empty_not_failed(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        """SPY is not a company: 404 and IndexError are translated to `empty`."""
        with Session(test_engine) as session:
            assert _count(session, "financial_periods", "SPY") == 0
            statuses = set(
                session.execute(
                    text(
                        "SELECT DISTINCT status FROM sync_run_items "
                        # bootstrap `symbols` is correctly `ok` for SPY: the
                        # symbol resolves, only financials data is missing.
                        "WHERE run_id = :r AND symbol = 'SPY' AND dataset <> 'symbols'"
                    ),
                    {"r": live_financials.run_id},
                ).scalars()
            )
        assert statuses == {"empty"}

    def test_fact_count_matches_item_count_sum(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        """The machine-checkable form of completeness."""
        with Session(test_engine) as session:
            facts = _count(session, "financial_facts")
            total = session.execute(
                text("SELECT COALESCE(SUM(item_count), 0) FROM financial_periods")
            ).scalar_one()
        assert facts == int(total)

    def test_currency_recorded_for_foreign_symbol(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        """THYAO.IS tables report in USD (its prices are in TRY)."""
        with Session(test_engine) as session:
            currencies = set(
                session.execute(
                    text("SELECT DISTINCT currency FROM financial_periods WHERE symbol='THYAO.IS'")
                ).scalars()
            )
        assert currencies and None not in currencies

    def test_sec_filings_only_for_us_symbols(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        with Session(test_engine) as session:
            assert _count(session, "sec_filings", "AAPL") > 0
            assert _count(session, "sec_filings", "THYAO.IS") == 0  # source returns {}

    def test_earnings_dates_written_for_companies(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        with Session(test_engine) as session:
            assert _count(session, "earnings_dates", "AAPL") > 0
            assert _count(session, "earnings_dates", "SPY") == 0  # source returns None

    def test_second_run_is_idempotent_and_skips(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, live_financials
    ) -> None:
        """A second run leaves the same row count and marks periods `skipped`
        (content_hash unchanged)."""
        with Session(test_engine) as session:
            before = _count(session, "financial_facts")

        datasets = SYMBOL_DATASETS.resolve(["financials"])
        summary = run_sync(test_engine, list(SYMBOLS), datasets)

        with Session(test_engine) as session:
            after = _count(session, "financial_facts")
        assert after == before
        assert summary.exit_code() == EXIT_OK
        # Hash unchanged -> items were not rewritten
        assert summary.totals["rows_skipped"] > 0


class TestLiveMarket:
    @pytest.fixture(scope="class")
    def market_summary_run(self, test_engine: Engine):  # type: ignore[no-untyped-def]
        return run_market_sync(test_engine, MARKET_DATASETS.resolve(None))

    def test_market_run_succeeds(self, market_summary_run) -> None:  # type: ignore[no-untyped-def]
        assert market_summary_run.exit_code() == EXIT_OK
        assert market_summary_run.symbol_count == 0  # region count is not written

    def test_only_us_has_status(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, market_summary_run
    ) -> None:
        """Status is None in 7 regions (deterministic, not flaky)."""
        with Session(test_engine) as session:
            regions = set(session.execute(text("SELECT region FROM market_status")).scalars())
        assert regions == {"US"}

    def test_every_region_has_summary(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, market_summary_run
    ) -> None:
        with Session(test_engine) as session:
            regions = set(
                session.execute(text("SELECT DISTINCT region FROM market_summary")).scalars()
            )
        assert len(regions) >= 6

    def test_calendars_written(  # type: ignore[no-untyped-def]
        self, test_engine: Engine, market_summary_run
    ) -> None:
        with Session(test_engine) as session:
            total = sum(
                _count(session, table)
                for table in ("calendar_earnings", "calendar_economic", "calendar_splits")
            )
        assert total > 0
