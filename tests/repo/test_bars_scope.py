"""intraday_scope resolution and the scope gate.

The asymmetry is deliberate and dangerous, hence pinned by tests:
  1m       -> empty table means NO symbol is in scope (1.21B rows/year)
  others   -> empty table means the WHOLE universe is in scope
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.errors import DatasetOutOfScope
from yfin.models import IntradayScope, Symbol
from yfin.pipeline.readers import GapReader, ScopeReader

pytestmark = pytest.mark.repo

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(db_session: Session) -> sessionmaker[Session]:
    """ScopeReader opens its own session; bind it to the same connection here,
    or it won't see fixture data that gets rolled back."""
    return sessionmaker(bind=db_session.connection(), expire_on_commit=False)


def _symbol(session: Session, name: str) -> None:
    session.add(Symbol(symbol=name, is_active=True))
    session.flush()


def _scope(session: Session, symbol: str, interval: str, *, enabled: bool = True) -> None:
    session.add(IntradayScope(symbol=symbol, bar_interval=interval, enabled=enabled, added_at=NOW))
    session.flush()


def test_1m_with_empty_table_covers_nobody(
    db_session: Session, factory: sessionmaker[Session]
) -> None:
    """1m opening to the whole universe on an empty table would mean 1.21B
    rows/year; failing to run is the safe side to fall to."""
    _symbol(db_session, "AAPL")

    assert ScopeReader(factory)("AAPL", "1m") is False


def test_other_intervals_with_empty_table_cover_everyone(
    db_session: Session, factory: sessionmaker[Session]
) -> None:
    _symbol(db_session, "AAPL")
    reader = ScopeReader(factory)

    for interval in ("5m", "15m", "60m", "1wk", "1mo"):
        assert reader("AAPL", interval) is True


def test_listed_symbol_is_in_scope(db_session: Session, factory: sessionmaker[Session]) -> None:
    _symbol(db_session, "AAPL")
    _scope(db_session, "AAPL", "1m")

    assert ScopeReader(factory)("AAPL", "1m") is True


def test_unlisted_symbol_is_out_of_scope_once_the_interval_has_any_row(
    db_session: Session, factory: sessionmaker[Session]
) -> None:
    """Dangerous asymmetry: adding one row for 5m pushes every other symbol
    out of scope. This test documents that."""
    _symbol(db_session, "AAPL")
    _symbol(db_session, "MSFT")
    _scope(db_session, "AAPL", "5m")
    reader = ScopeReader(factory)

    assert reader("AAPL", "5m") is True
    assert reader("MSFT", "5m") is False


def test_disabled_row_still_counts_as_a_registered_interval(
    db_session: Session, factory: sessionmaker[Session]
) -> None:
    """An interval with only enabled=0 rows still counts as "has a record" ->
    no symbol runs."""
    _symbol(db_session, "AAPL")
    _scope(db_session, "AAPL", "5m", enabled=False)

    assert ScopeReader(factory)("AAPL", "5m") is False


def test_scope_is_read_once_per_run(db_session: Session, factory: sessionmaker[Session]) -> None:
    """The set is cached on the instance: one query per symbol per run would
    mean ~5,000 queries."""
    _symbol(db_session, "AAPL")
    _scope(db_session, "AAPL", "1m")
    reader = ScopeReader(factory)

    assert reader("AAPL", "1m") is True
    # The cache does not change even after a second symbol is added
    _symbol(db_session, "MSFT")
    _scope(db_session, "MSFT", "1m")
    assert reader("MSFT", "1m") is False, "scope set was re-read"


def test_gap_reader_returns_only_unresolved_fetch_failures(
    db_session: Session, factory: sessionmaker[Session]
) -> None:
    _symbol(db_session, "AAPL")
    db_session.execute(
        text(
            "INSERT INTO bar_gaps (symbol, bar_interval, gap_start_utc, gap_end_utc,"
            " detected_at, reason, resolved_at) VALUES"
            " ('AAPL','1m','2026-08-01','2026-08-02','2026-08-02','fetch_failed',NULL),"
            " ('AAPL','1m','2026-08-05','2026-08-06','2026-08-06','fetch_failed','2026-08-07'),"
            " ('AAPL','1m','2026-07-01','2026-07-02','2026-07-02','retention_expired',NULL)"
        )
    )
    db_session.flush()

    gaps = GapReader(factory)("AAPL", "1m")

    # Resolved gaps and retention_expired gaps are excluded: the first is no
    # longer actionable, the second can no longer be fetched at all.
    assert [g[0] for g in gaps] == [datetime(2026, 8, 1, tzinfo=UTC)]


def test_out_of_scope_exception_is_not_a_value_error() -> None:
    """errors._NEVER_RETRYABLE includes ValueError; if this inherited from it,
    being out of scope would be silently classified as a data error."""
    exc = DatasetOutOfScope("1m")

    assert not isinstance(exc, ValueError)
    assert exc.interval == "1m"


def test_purge_removes_price_bars_leaving_no_orphans(db_session: Session) -> None:
    """price_bars carries an FK to symbols, so purge finds it by following
    FK edges and deletes the bars with the symbol.

    It did not always: while the table was partitioned it could not carry
    an FK, so purge had to be told about it by hand or the bars were left
    orphaned with no warning -- while bar_rescales, which did have an FK,
    was deleted and took the rescale ledger with it. A hypertable can be
    the referencing side, so that special case is gone.
    """
    from sqlalchemy import delete

    from yfin.models import Base, PriceBar, symbol_scoped_tables

    _symbol(db_session, "AAPL")
    db_session.add(
        PriceBar(
            symbol="AAPL",
            bar_interval="5m",
            ts_utc=datetime(2026, 9, 2, 14, 30),
            local_date=datetime(2026, 9, 2).date(),
            close=Decimal("100"),
            volume=1,
            is_extended=False,
        )
    )
    db_session.flush()

    names = symbol_scoped_tables()
    assert "price_bars" in names, "purge does not see price_bars at all"
    # The ledger must be deleted last
    assert names.index("price_bars") < names.index("bar_rescales")

    for name in names:
        table = Base.metadata.tables[name]
        db_session.execute(delete(table).where(table.c["symbol"] == "AAPL"))
    db_session.flush()

    remaining = db_session.execute(
        text("SELECT COUNT(*) FROM price_bars WHERE symbol = 'AAPL'")
    ).scalar_one()
    assert remaining == 0
