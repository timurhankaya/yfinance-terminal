"""Repository testleri: gercek MySQL 8.3, agsiz (S9.2).

Her test kendi transaction'inda calisir ve sonunda rollback edilir.
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
        # Ikinci calistirma da 'ok' vermelidir (S8.6)
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
        """symbols dataset'i isin kolonuna HIC dokunmaz (S6.1/3)."""
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
        """ROW_COUNT() degismeyen satirda 0 doner; anahtar varligi 1 (S8.6)."""
        _seed_symbol(db_session)
        write = _price_write([_row(5, "3.0")])
        apply_write(PostgresRowWriter(db_session), write, WriteStats())

        stats = WriteStats()
        apply_write(PostgresRowWriter(db_session), write, stats)  # birebir ayni satir
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
        """DB'den geri okunarak dogrulanir (S5.4)."""
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
        """DATETIME(6): DATETIME(0) ayni saniyede PK cakismasi uretirdi."""
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
        assert stored == stamp  # timestamptz(0) 10:00:01'e YUVARLARDI


class TestCollation:
    def test_ascii_bin_distinguishes_case(self, db_session: Session) -> None:
        """utf8mb4_0900_ai_ci 'AAPL' = 'aapl' derdi (S5.1)."""
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
        """ON DELETE RESTRICT soft-delete politikasini DB'de zorlar (S5.5)."""
        _seed_symbol(db_session)
        apply_write(PostgresRowWriter(db_session), _price_write([_row(7, "1.0")]), WriteStats())
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(text("DELETE FROM symbols WHERE symbol = 'AAPL'"))
            db_session.flush()

    def test_news_symbols_accepts_unknown_symbol(self, db_session: Session) -> None:
        """news_symbols.symbol'da FK yoktur; evren disi sembol transaction'i
        dusurmemelidir (S5.5)."""
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
        """FK olsaydi unknown_symbol kaydi ERROR 1452 verirdi (S5.5)."""
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
    def test_longtext_is_byte_for_byte_faithful(self, db_session: Session) -> None:
        """JSON tipi anahtar sirasini bozar, NaN'i reddeder, float'i kirpar."""
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
        # anahtar sirasi ve hassasiyet korunur
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
                # MySQL SHA2(x, 256) -> PG encode(sha256(x::bytea), 'hex').
                # `::bytea` cast SART: sha256 bytea alir, text degil.
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
