"""Sharded write behavior: monotonic columns and lock conflicts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from yfin.datasets.base import NormalizedResult
from yfin.models import PriceHistory, Symbol
from yfin.pipeline.payload import SymbolPayload
from yfin.pipeline.persist import is_lock_conflict, persist_with_retry
from yfin.storage.contracts import TableWrite, WriteStats
from yfin.storage.persistence import PostgresRowWriter

pytestmark = pytest.mark.repo

SYMBOL = "ZZTEST"


def _price_write(session_date: date, *, repaired: bool) -> TableWrite:
    return TableWrite(
        table="price_history",
        rows=[
            {
                "symbol": SYMBOL,
                "session_date": session_date,
                "ts_utc": datetime(2026, 1, 2, 21, 0),
                "close": Decimal("100.5"),
                "dividend": Decimal(0),
                "split_ratio": Decimal(0),
                "capital_gain": Decimal(0),
                "is_repaired": repaired,
            }
        ],
        key_columns=("symbol", "session_date"),
        update_columns=("ts_utc", "close", "is_repaired"),
        monotonic_columns=("is_repaired",),
    )


@pytest.fixture
def seeded_symbol(test_engine: Engine) -> Any:
    with Session(test_engine) as session:
        session.merge(Symbol(symbol=SYMBOL, is_active=True))
        session.commit()
    yield
    with test_engine.connect() as conn:
        conn.execute(text("DELETE FROM price_history WHERE symbol = :s"), {"s": SYMBOL})
        conn.execute(text("DELETE FROM symbols WHERE symbol = :s"), {"s": SYMBOL})
        conn.commit()


@pytest.mark.usefixtures("seeded_symbol")
class TestMonotonicRepairColumn:
    """Repair heuristics depend on window length; in a narrow incremental
    window the same row can come back as 1 once and 0 the next time."""

    def test_repaired_flag_never_regresses(self, test_engine: Engine) -> None:
        day = date(2026, 1, 2)
        with Session(test_engine) as session:
            writer = PostgresRowWriter(session)
            writer.write(_price_write(day, repaired=True))
            session.commit()

            # Second run reports the same row as not repaired
            writer.write(_price_write(day, repaired=False))
            session.commit()

            row = session.execute(
                select(PriceHistory).where(
                    PriceHistory.symbol == SYMBOL, PriceHistory.session_date == day
                )
            ).scalar_one()
            assert row.is_repaired is True, "GREATEST must not write the information back"

    def test_flag_still_rises_from_zero(self, test_engine: Engine) -> None:
        day = date(2026, 1, 5)
        with Session(test_engine) as session:
            writer = PostgresRowWriter(session)
            writer.write(_price_write(day, repaired=False))
            session.commit()
            writer.write(_price_write(day, repaired=True))
            session.commit()

            row = session.execute(
                select(PriceHistory).where(
                    PriceHistory.symbol == SYMBOL, PriceHistory.session_date == day
                )
            ).scalar_one()
            assert row.is_repaired is True


class _FakeDbapiError(Exception):
    """Mimics the shape of `DBAPIError.orig`.

    SQLAlchemy exposes the driver exception under `.orig`, and psycopg3
    exceptions carry `.sqlstate`; classification looks at this, not the
    error text.
    """

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"fake error {sqlstate}")
        self.orig = type("_Orig", (), {"sqlstate": sqlstate})()


class TestLockConflictClassification:
    @pytest.mark.parametrize(
        "sqlstate",
        [
            "40001",  # serialization_failure
            "40P01",  # deadlock_detected
        ],
    )
    def test_lock_sqlstates_are_retryable(self, sqlstate: str) -> None:
        assert is_lock_conflict(_FakeDbapiError(sqlstate))

    def test_other_sqlstates_are_not_retried(self) -> None:
        # 42703 undefined_column -- a programming error; retrying would give
        # the same result forever.
        assert not is_lock_conflict(_FakeDbapiError("42703"))

    def test_55p03_is_deliberately_excluded(self) -> None:
        """lock_not_available is deliberately excluded: this code path uses
        no NOWAIT / SKIP LOCKED, so it never occurs. Retrying an SQLSTATE
        with no justification would silently legitimize wrong behavior if
        NOWAIT were added later."""
        assert not is_lock_conflict(_FakeDbapiError("55P03"))

    def test_exception_without_orig_is_not_retried(self) -> None:
        """An exception with no `orig` is a programming error; the getattr
        chain returns None and it is not retried."""
        assert not is_lock_conflict(RuntimeError("plain error"))


class _FlakyDataset:
    """Lock conflict on the first attempt, success on the second.

    Symbols are distributed across shards, so two processes can write to the
    same news / news_symbols row; a single process has no such risk.
    """

    name = "flaky"
    produces = ("price_history",)

    def __init__(self) -> None:
        self.calls = 0

    def upsert(
        self, writer: Any, result: NormalizedResult, *, full_refresh: bool = False
    ) -> WriteStats:
        self.calls += 1
        if self.calls == 1:
            raise _FakeDbapiError("40P01")
        stats = WriteStats()
        for write in result.writes:
            stats.attempted[write.table] = len(write.rows)
            stats.verified[write.table] = writer.write(write)
        return stats


@pytest.mark.usefixtures("seeded_symbol")
class TestTransactionRetry:
    def test_lock_conflict_is_retried_and_succeeds(self, test_engine: Engine) -> None:
        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
        dataset = _FlakyDataset()
        result = NormalizedResult(writes=[_price_write(date(2026, 2, 2), repaired=False)])
        payload = SymbolPayload(symbol=SYMBOL, resolved=True)
        payload.results.append((dataset, result, 1, 0))  # type: ignore[arg-type]

        records = persist_with_retry(factory, payload, attempts=3)

        assert dataset.calls == 2, "the first attempt must fail with a lock conflict"
        assert all(r.status.value != "failed" for r in records)
        with Session(test_engine) as session:
            assert (
                session.execute(
                    select(PriceHistory).where(
                        PriceHistory.symbol == SYMBOL,
                        PriceHistory.session_date == date(2026, 2, 2),
                    )
                ).scalar_one_or_none()
                is not None
            )

    def test_non_lock_error_is_not_retried(self, test_engine: Engine) -> None:
        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

        class _Broken(_FlakyDataset):
            def upsert(
                self,
                writer: Any,
                result: NormalizedResult,
                *,
                full_refresh: bool = False,
            ) -> WriteStats:
                self.calls += 1
                raise RuntimeError("(1054, \"Unknown column 'nope'\")")

        dataset = _Broken()
        payload = SymbolPayload(symbol=SYMBOL, resolved=True)
        payload.results.append(
            (dataset, NormalizedResult(writes=[]), 0, 0)  # type: ignore[arg-type]
        )

        records = persist_with_retry(factory, payload, attempts=3)
        assert dataset.calls == 1, "a deterministic error must not be retried"
        assert all(r.status.value == "failed" for r in records)


@pytest.mark.usefixtures("seeded_symbol")
class TestFailedTransactionAudit:
    """Regression test: even if the transaction fails, all three channels stay audited."""

    class _Exploding:
        name = "boom"
        produces = ("price_history",)

        def upsert(self, writer, result, *, full_refresh=False):  # type: ignore[no-untyped-def]
            raise RuntimeError("write failed")

    def test_failures_and_skipped_survive_a_failed_transaction(
        self, test_engine: Engine
    ) -> None:
        factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
        payload = SymbolPayload(symbol=SYMBOL, resolved=True)
        payload.results.append((self._Exploding(), NormalizedResult(), 1, 0))  # type: ignore[arg-type]
        payload.failures.append(("info", "HTTPError: 500"))
        payload.skipped.append(("news", "date_range=none"))
        payload.out_of_scope.append(("bars_1m", "outside intraday_scope"))

        records = persist_with_retry(factory, payload, attempts=1)

        by_dataset = {r.dataset: r.status.value for r in records}
        # `boom` fails because the write failed, but the other three channels survive
        assert by_dataset["boom"] == "failed"
        assert by_dataset["info"] == "failed"
        assert by_dataset["news"] == "skipped"
        assert by_dataset["bars_1m"] == "out_of_scope"

