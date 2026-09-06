"""Repository tests: real PostgreSQL + TimescaleDB, no network.

Each test runs in its own transaction and is rolled back at the end.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from yfin import normalize as nz
from yfin.datasets.base import TableWrite, WriteStats
from yfin.models import Base, PriceHistory, Symbol
from yfin.persistence import PostgresRowWriter, apply_write

pytestmark = pytest.mark.repo


def _seed_symbol(session: Session, symbol: str = "AAPL") -> None:
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, now(), now()) "
            "ON CONFLICT (symbol) DO NOTHING"
        ),
        {"s": symbol},
    )


def _price_write(rows: list[dict]) -> TableWrite:  # type: ignore[type-arg]
    return TableWrite(
        table="price_history",
        rows=rows,
        key_columns=("symbol", "session_date"),
        update_columns=("ts_utc", "close", "volume"),
    )


def _row(day: int, close: str, volume: int = 100) -> dict:  # type: ignore[type-arg]
    return {
        "symbol": "AAPL",
        "session_date": date(2026, 1, day),
        "ts_utc": datetime(2026, 1, day, 21, 0),
        "close": Decimal(close),
        "volume": volume,
    }


class TestIdempotency:
    def test_writing_twice_does_not_change_table(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        write = _price_write([_row(2, "1.5"), _row(3, "2.5")])

        stats = WriteStats()
        apply_write(PostgresRowWriter(db_session), write, stats)
        first = db_session.execute(select(func.count()).select_from(PriceHistory)).scalar_one()

        stats2 = WriteStats()
        apply_write(PostgresRowWriter(db_session), write, stats2)
        second = db_session.execute(select(func.count()).select_from(PriceHistory)).scalar_one()

        assert first == second == 2
        # The second run must also report 'ok'
        assert stats2.verified["price_history"] == stats2.attempted["price_history"] == 2

    def test_changed_value_is_overwritten(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        stats = WriteStats()
        apply_write(PostgresRowWriter(db_session), _price_write([_row(2, "1.5")]), stats)
        apply_write(PostgresRowWriter(db_session), _price_write([_row(2, "9.99")]), WriteStats())
        close = db_session.execute(
            select(PriceHistory.close).where(PriceHistory.session_date == date(2026, 1, 2))
        ).scalar_one()
        assert close == Decimal("9.99")


class TestUpdateColumnScope:
    def test_symbols_run_does_not_null_isin(self, db_session: Session) -> None:
        """The symbols dataset never touches the isin column."""
        _seed_symbol(db_session)
        apply_write(
            PostgresRowWriter(db_session),
            TableWrite(
                table="symbols",
                rows=[{"symbol": "AAPL", "isin": "US0378331005"}],
                key_columns=("symbol",),
                update_columns=("isin",),
            ),
            WriteStats(),
        )
        apply_write(
            PostgresRowWriter(db_session),
            TableWrite(
                table="symbols",
                rows=[{"symbol": "AAPL", "currency": "USD", "quote_type": "EQUITY"}],
                key_columns=("symbol",),
                update_columns=("currency", "quote_type"),
            ),
            WriteStats(),
        )
        row = db_session.get(Symbol, "AAPL")
        assert row is not None
        assert row.isin == "US0378331005"
        assert row.currency == "USD"


class TestVerification:
    def test_verified_uses_key_existence_not_row_count(self, db_session: Session) -> None:
        """ROW_COUNT() returns 0 for an unchanged row; key-existence returns 1."""
        _seed_symbol(db_session)
        write = _price_write([_row(5, "3.0")])
        apply_write(PostgresRowWriter(db_session), write, WriteStats())

        stats = WriteStats()
        apply_write(PostgresRowWriter(db_session), write, stats)  # identical row
        assert stats.verified["price_history"] == 1
        assert stats.attempted["price_history"] == 1

    def test_composite_key_verification(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        rows = [_row(d, "1.0") for d in range(1, 21)]
        stats = WriteStats()
        apply_write(PostgresRowWriter(db_session), _price_write(rows), stats)
        assert stats.verified["price_history"] == 20


class TestDecimalPrecision:
    def test_no_rounding_loss(self, db_session: Session) -> None:
        """Verified by reading the value back from the DB."""
        _seed_symbol(db_session)
        values = ["0.128348", "0.001870", "1234567890.123456789012"]
        rows = [{**_row(10 + i, v), "close": nz.to_decimal(float(v))} for i, v in enumerate(values)]
        apply_write(PostgresRowWriter(db_session), _price_write(rows), WriteStats())
        stored = (
            db_session.execute(select(PriceHistory.close).order_by(PriceHistory.session_date))
            .scalars()
            .all()
        )
        assert stored[0] == Decimal("0.128348")
        assert stored[1] == Decimal("0.001870")


class TestSnapshotTimestamps:
    def test_two_snapshots_in_same_second(self, db_session: Session) -> None:
        """TIMESTAMP(6): a second-precision timestamp would collide within the same second."""
        _seed_symbol(db_session)
        table = Base.metadata.tables["ticker_fast_info_history"]
        base = datetime(2026, 1, 1, 10, 0, 0)
        for micro in (750000, 750001):
            db_session.execute(
                table.insert().values(
                    symbol="AAPL",
                    fetched_at=base.replace(microsecond=micro),
                    raw_json="{}",
                    content_hash="a" * 64,
                )
            )
        count = db_session.execute(
            select(func.count()).select_from(table).where(table.c.symbol == "AAPL")
        ).scalar_one()
        assert count == 2

    def test_fractional_second_not_rounded(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        table = Base.metadata.tables["ticker_fast_info_history"]
        stamp = datetime(2026, 1, 1, 10, 0, 0, 750000, tzinfo=UTC)
        db_session.execute(
            table.insert().values(
                symbol="AAPL", fetched_at=stamp, raw_json="{}", content_hash="b" * 64
            )
        )
        stored = db_session.execute(
            select(table.c.fetched_at).where(table.c.symbol == "AAPL")
        ).scalar_one()
        assert stored == stamp  # timestamptz(0) would round to 10:00:01


class TestCollation:
    def test_symbol_collation_distinguishes_case(self, db_session: Session) -> None:
        """A case-insensitive collation would treat 'AAPL' == 'aapl';
        COLLATE "C" keeps them distinct."""
        _seed_symbol(db_session, "AAPL")
        _seed_symbol(db_session, "aapl")
        count = db_session.execute(
            select(func.count()).select_from(Symbol).where(Symbol.symbol.in_(["AAPL", "aapl"]))
        ).scalar_one()
        assert count == 2

    def test_officer_name_is_case_sensitive(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        table = Base.metadata.tables["company_officers"]
        db_session.execute(table.insert().values(symbol="AAPL", name="Tim Cook"))
        db_session.execute(table.insert().values(symbol="AAPL", name="TIM COOK"))
        count = db_session.execute(
            select(func.count()).select_from(table).where(table.c.symbol == "AAPL")
        ).scalar_one()
        assert count == 2


class TestForeignKeys:
    def test_delete_restricted_by_child_rows(self, db_session: Session) -> None:
        """ON DELETE RESTRICT enforces the soft-delete policy at the DB level."""
        _seed_symbol(db_session)
        apply_write(PostgresRowWriter(db_session), _price_write([_row(7, "1.0")]), WriteStats())
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(text("DELETE FROM symbols WHERE symbol = 'AAPL'"))
            db_session.flush()

    def test_news_symbols_accepts_unknown_symbol(self, db_session: Session) -> None:
        """news_symbols.symbol has no FK; an out-of-universe symbol must not
        fail the transaction."""
        news = Base.metadata.tables["news"]
        links = Base.metadata.tables["news_symbols"]
        news_id = "11111111-2222-3333-4444-555555555555"
        db_session.execute(
            news.insert().values(
                news_id=news_id,
                title="t",
                pub_date=datetime(2026, 1, 1),
                raw_json="{}",
            )
        )
        db_session.execute(
            links.insert().values(news_id=news_id, symbol="005930.KS", is_known=False)
        )
        stored = db_session.execute(
            select(links.c.is_known).where(links.c.news_id == news_id)
        ).scalar_one()
        assert stored == 0

    def test_sync_run_items_accepts_unknown_symbol(self, db_session: Session) -> None:
        """If an FK existed, an unknown_symbol record would raise a constraint error."""
        runs = Base.metadata.tables["sync_runs"]
        items = Base.metadata.tables["sync_run_items"]
        result = db_session.execute(
            runs.insert().values(started_at=datetime(2026, 1, 1), status="running")
        )
        run_id = result.inserted_primary_key[0]
        db_session.execute(
            items.insert().values(
                run_id=run_id, symbol="NOSUCHSYM", dataset="symbols", status="unknown_symbol"
            )
        )
        count = db_session.execute(
            select(func.count()).select_from(items).where(items.c.run_id == run_id)
        ).scalar_one()
        assert count == 1


class TestRawJson:
    def test_raw_json_is_byte_for_byte_faithful(self, db_session: Session) -> None:
        """The JSON type reorders keys, rejects NaN, and truncates floats."""
        _seed_symbol(db_session)
        payload = {"z": 1, "a": 0.001870, "t": "Türkçe", "n": float("nan")}
        canonical = nz.canonical_json(payload)
        digest = nz.content_hash(canonical=canonical)

        table = Base.metadata.tables["ticker_fast_info"]
        db_session.execute(
            table.insert().values(
                symbol="AAPL",
                raw_json=canonical,
                content_hash=digest,
                fetched_at=datetime(2026, 1, 1),
            )
        )
        stored, stored_hash = db_session.execute(
            select(table.c.raw_json, table.c.content_hash).where(table.c.symbol == "AAPL")
        ).one()
        assert stored == canonical
        assert nz.content_hash(canonical=stored) == stored_hash
        # key order and precision are preserved
        assert stored.index('"a"') < stored.index('"z"')
        assert "0.00187" in stored

    def test_hash_verifiable_in_sql(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        canonical = nz.canonical_json({"a": 1})
        table = Base.metadata.tables["ticker_fast_info"]
        db_session.execute(
            table.insert().values(
                symbol="AAPL",
                raw_json=canonical,
                content_hash=nz.content_hash(canonical=canonical),
                fetched_at=datetime(2026, 1, 1),
            )
        )
        matches = db_session.execute(
            text(
                # PG: encode(sha256(x::bytea), 'hex').
                # The `::bytea` cast is required: sha256 takes bytea, not text.
                "SELECT encode(sha256(raw_json::bytea), 'hex') = content_hash "
                "FROM ticker_fast_info "
                "WHERE symbol = 'AAPL'"
            )
        ).scalar_one()
        assert matches == 1


class TestReplaceScope:
    def test_departed_officer_is_removed(self, db_session: Session) -> None:
        _seed_symbol(db_session)

        def officers(names: list[str]) -> TableWrite:
            return TableWrite(
                table="company_officers",
                rows=[{"symbol": "AAPL", "name": n} for n in names],
                key_columns=("symbol", "name"),
                update_columns=("title",),
                mode="replace_scope",
            )

        apply_write(PostgresRowWriter(db_session), officers(["A", "B"]), WriteStats())
        apply_write(PostgresRowWriter(db_session), officers(["B", "C"]), WriteStats())
        table = Base.metadata.tables["company_officers"]
        names = set(
            db_session.execute(select(table.c.name).where(table.c.symbol == "AAPL")).scalars()
        )
        assert names == {"B", "C"}


class TestView:
    def test_v_actions_unions_three_tables(self, db_session: Session) -> None:
        _seed_symbol(db_session)
        db_session.execute(
            Base.metadata.tables["dividends"]
            .insert()
            .values(symbol="AAPL", ex_date=date(2026, 1, 1), amount=Decimal("0.25"))
        )
        db_session.execute(
            Base.metadata.tables["splits"]
            .insert()
            .values(symbol="AAPL", split_date=date(2026, 2, 1), ratio=Decimal(4))
        )
        db_session.execute(
            Base.metadata.tables["capital_gains"]
            .insert()
            .values(symbol="AAPL", gain_date=date(2026, 3, 1), amount=Decimal("1.5"))
        )
        rows = db_session.execute(
            text(
                "SELECT action_type, action_value FROM v_actions WHERE symbol='AAPL' "
                "ORDER BY action_date"
            )
        ).all()
        assert [r[0] for r in rows] == ["DIVIDEND", "SPLIT", "CAPITAL_GAIN"]
        assert rows[0][1] == Decimal("0.25")


@pytest.mark.repo
class TestPostgresUpsertSemantics:
    """Two points whose behavior changed on the move to PostgreSQL.

    Both used to run silently differently under MySQL; this pins the
    behavior against the real engine.
    """

    def test_greatest_ignores_null_and_orders_booleans(self, db_session: Session) -> None:
        """MySQL's `GREATEST(x, NULL)` returned NULL; PostgreSQL ignores NULL.
        The monotonic column (price_history.is_repaired) depends on this, and
        PostgreSQL's behavior is safer: the stored value survives even if the
        source reports NULL once."""
        row = db_session.execute(
            text(
                "SELECT greatest(true, NULL::boolean) AS a, "
                "       greatest(false, true) AS b, "
                "       greatest(5, NULL::int) AS c"
            )
        ).one()
        assert row.a is True
        assert row.b is True  # false < true ordering is defined
        assert row.c == 5

    def test_repeated_key_in_one_write_does_not_raise(self, db_session: Session) -> None:
        """Without dedup, this would raise 21000 cardinality_violation:
        `ON CONFLICT DO UPDATE` cannot touch the same row twice in one
        statement. MySQL swallowed this without complaint."""
        _seed_symbol(db_session)
        rows = [_row(1, "1.0"), _row(1, "2.5")]  # same key, two rows
        assert rows[0]["session_date"] == rows[1]["session_date"]

        stats = WriteStats()
        apply_write(PostgresRowWriter(db_session), _price_write(rows), stats)
        db_session.flush()

        close = db_session.execute(
            text("SELECT close FROM price_history WHERE symbol = 'AAPL'")
        ).scalar_one()
        assert close == Decimal("2.5000000000000000000000000000"), "last row must win"

    def test_monotonic_column_never_regresses(self, db_session: Session) -> None:
        """If True then False is written for the same row, the value must
        stay True: source repair heuristics can report the same bar as 1
        then 0 on a later run."""
        _seed_symbol(db_session)
        writer = PostgresRowWriter(db_session)
        first = _row(2, "1.0")
        first["is_repaired"] = True
        apply_write(writer, _price_write([first]), WriteStats())
        db_session.flush()

        second = _row(2, "1.0")
        second["is_repaired"] = False
        apply_write(writer, _price_write([second]), WriteStats())
        db_session.flush()

        repaired = db_session.execute(
            text("SELECT is_repaired FROM price_history WHERE symbol = 'AAPL' AND close = 1.0")
        ).scalar_one()
        assert repaired is True, "GREATEST must preserve monotonicity"
