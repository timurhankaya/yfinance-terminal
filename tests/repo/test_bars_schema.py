"""price_bars sema davranisi: hypertable, chunk, view (PB S9.2, PG S7.1).

Bu dosya SEMA testleridir -- yazma yolunu degil, tablonun gercekten
iddia edildigi gibi davrandigini dogrular. Hypertable'lar `create_all()`
ile OLUSMAZ; conftest migration ile AYNI `timescale_ddl()` sabitini
uygular ve bu testler o sabitin isini yaptigini kanitlar.

MySQL'de burada aylik RANGE COLUMNS partition'lari, MAXVALUE'suz
tasarim ve "aralik disi insert ERROR 1526 ile duser" davranisi test
ediliyordu. TimescaleDB chunk'lari YAZMA ANINDA olusturdugu icin o
kavramlarin hicbiri yoktur; testin AMACI ayni kaldi -- bolumleme
gercekten kurulu mu, chunk'lar olusuyor mu, sorgu chunk'a iniyor mu.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.models import PriceBar

pytestmark = pytest.mark.repo


def _bar(ts: datetime, *, symbol: str = "AAPL", interval: str = "5m") -> PriceBar:
    return PriceBar(
        symbol=symbol,
        bar_interval=interval,
        ts_utc=ts,
        local_date=ts.date(),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=1000,
        is_extended=False,
    )


def _seed_symbol(session: Session, symbol: str = "AAPL") -> None:
    """price_bars artik symbols'a FK TASIYOR (PG S7.2).

    MySQL'de partition yuzunden FK yoktu ve barlar oksuz yazilabiliyordu;
    artik ebeveyn satir ONCE var olmali.
    """
    session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, true, 0, now(), now()) ON CONFLICT (symbol) DO NOTHING"
        ),
        {"s": symbol},
    )


def test_price_bars_is_a_hypertable(db_session: Session) -> None:
    """Bolumleme kolonu ve chunk araligi beklendigi gibi olmali."""
    row = db_session.execute(
        text(
            # `hypertable_schema` filtresi SART: view semaya gore
            # filtrelemez ve ayni ad hem `public`te (uretim) hem surece
            # ozel test semasinda bulunur -> MultipleResultsFound.
            "SELECT column_name, column_type, time_interval "
            "  FROM timescaledb_information.dimensions "
            " WHERE hypertable_schema = current_schema() "
            "   AND hypertable_name = 'price_bars'"
        )
    ).one()

    assert row.column_name == "ts_utc"
    assert "time zone" in row.column_type, "bolumleme kolonu timestamptz olmali"
    assert row.time_interval == timedelta(days=7)


def test_price_history_is_a_hypertable(db_session: Session) -> None:
    row = db_session.execute(
        text(
            "SELECT column_name, time_interval "
            "  FROM timescaledb_information.dimensions "
            " WHERE hypertable_schema = current_schema() "
            "   AND hypertable_name = 'price_history'"
        )
    ).one()

    assert row.column_name == "session_date"
    assert row.time_interval == timedelta(days=365)


def test_no_default_partition_index_is_created(db_session: Session) -> None:
    """`create_default_indexes => FALSE` OLMADAN Alembic'in "bos diff"
    kapisi HIC acilmaz (PG S7.1): varsayilan DESC indeks `public`
    semasinda durur, Base.metadata'da yoktur ve autogenerate onu
    "silinmeli" diye raporlar."""
    names = set(
        db_session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                " WHERE tablename IN ('price_bars', 'price_history')"
            )
        )
        .scalars()
        .all()
    )

    assert "price_bars_ts_utc_idx" not in names
    assert "price_history_session_date_idx" not in names


def test_chunks_are_created_on_write(committed_session: Session, cleanup_tables: list[str]) -> None:
    """MySQL'de aylik partition'lari ELLE eklemek gerekiyordu ve bakim
    atlanirsa insert ERROR 1526 ile duserdi. TimescaleDB chunk'i yazma
    aninda kendisi acar; "aralik disi insert" kavrami YOKTUR -- 2029
    tarihli bir bar da sorunsuz yazilir."""
    cleanup_tables.extend(["price_bars", "symbols"])
    _seed_symbol(committed_session)
    committed_session.add(_bar(datetime(2026, 9, 2, 14, 30, tzinfo=UTC)))
    # Iki farkli chunk'a dusecek kadar uzak, ve GELECEK bir tarih
    committed_session.add(_bar(datetime(2029, 1, 2, 14, 30, tzinfo=UTC)))
    committed_session.commit()

    chunks = committed_session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            " WHERE hypertable_schema = current_schema() "
            "   AND hypertable_name = 'price_bars'"
        )
    ).scalar_one()

    assert chunks >= 2, "chunk'lar yazma aninda olusmali"


def test_range_query_descends_to_chunks(db_session: Session) -> None:
    """Baskin sorgu deseni chunk seviyesine inmeli (chunk exclusion)."""
    plan = "\n".join(
        db_session.execute(
            text(
                "EXPLAIN SELECT close FROM price_bars "
                "WHERE symbol = 'AAPL' AND bar_interval = '5m' "
                "AND ts_utc >= '2026-09-01' AND ts_utc < '2026-10-01'"
            )
        )
        .scalars()
        .all()
    )

    assert "_hyper_" in plan or "Chunk" in plan, f"chunk'a inilmedi:\n{plan}"


def test_view_hides_extended_session_bars(db_session: Session) -> None:
    """v_price_bars_regular seans disi barlari GIZLER (PB S5.10)."""
    _seed_symbol(db_session)
    regular = _bar(datetime(2026, 9, 2, 14, 30, tzinfo=UTC))
    extended = _bar(datetime(2026, 9, 2, 8, 5, tzinfo=UTC))
    extended.is_extended = True
    db_session.add_all([regular, extended])
    db_session.flush()

    seen = (
        db_session.execute(
            text(
                "SELECT ts_utc FROM v_price_bars_regular "
                "WHERE symbol = 'AAPL' AND bar_interval = '5m'"
            )
        )
        .scalars()
        .all()
    )

    assert seen == [datetime(2026, 9, 2, 14, 30, tzinfo=UTC)]


def test_orphan_bars_are_impossible(db_session: Session) -> None:
    """FK artik VAR: oksuz bar YAZILAMAZ (PG S7.2).

    MySQL'de bunun tersi test ediliyordu -- partition yuzunden FK
    tasinamadigi icin oksuz satir yazilabiliyor, butunluk aylik bir
    denetim sorgusuyla korunuyordu. Hypertable referencing taraf
    olabildigi icin o sorgu GEREKSIZLESTI ve `bars maintain`den
    kaldirildi.
    """
    from sqlalchemy.exc import IntegrityError

    db_session.add(_bar(datetime(2026, 9, 2, 14, 30, tzinfo=UTC), symbol="ZZNOSYMBOL"))
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()

    assert "fk_price_bars_symbol_symbols" in str(excinfo.value)
