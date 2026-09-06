"""price_bars sema davranisi: partition, pruning, view (PB S9.2).

Bu dosya SEMA testleridir - yazma yolunu degil, tablonun MySQL'de
gercekten iddia edildigi gibi davrandigini dogrular. Partition'lar
create_all() ile OLUSMAZ; conftest migration ile ayni
price_bars_partition_ddl() sabitini uygular ve bu testler o sabitin
isini yaptigini kanitlar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
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


def test_price_bars_is_partitioned_by_month(db_session: Session, test_schema: str) -> None:
    """p_hist + 60 aylik bolum; MAXVALUE bolumu YOK."""
    rows = (
        db_session.execute(
            text(
                "SELECT partition_name FROM information_schema.partitions "
                "WHERE table_schema = :s AND table_name = 'price_bars' "
                "AND partition_name IS NOT NULL"
            ),
            {"s": test_schema},
        )
        .scalars()
        .all()
    )

    assert len(rows) == 61, f"beklenen 61 partition, gelen {len(rows)}"
    assert "p_hist" in rows
    assert "p2026_09" in rows
    # MAXVALUE bolumu bilincli olarak YOKTUR (PB S5.3): varsa
    # ADD PARTITION imkansizlasir ve bakim atlandiginda pruning sessizce
    # olur. Yoklugunda aralik disi insert gurultulu sekilde duser.
    descriptions = (
        db_session.execute(
            text(
                "SELECT partition_description FROM information_schema.partitions "
                "WHERE table_schema = :s AND table_name = 'price_bars'"
            ),
            {"s": test_schema},
        )
        .scalars()
        .all()
    )
    assert not any(d and "MAXVALUE" in d.upper() for d in descriptions)


def test_range_query_prunes_to_one_partition(db_session: Session) -> None:
    """Baskin sorgu deseni tek partition'a inmeli."""
    plan = (
        db_session.execute(
            text(
                "EXPLAIN SELECT close FROM price_bars "
                "WHERE symbol = 'AAPL' AND bar_interval = '5m' "
                "AND ts_utc >= '2026-09-01' AND ts_utc < '2026-10-01'"
            )
        )
        .mappings()
        .first()
    )

    assert plan is not None
    assert plan["partitions"] == "p2026_09", f"partition pruning calismadi: {plan['partitions']}"


def test_insert_outside_partition_range_fails_loudly(db_session: Session) -> None:
    """MAXVALUE'suz tasarimin TA KENDISI: aralik disi satir sessizce
    birikmez, ERROR 1526 ile duser ve bakimin atlandigini kosuda bildirir."""
    with pytest.raises(OperationalError) as excinfo:
        db_session.add(_bar(datetime(2029, 1, 2, 14, 30, tzinfo=UTC).replace(tzinfo=None)))
        db_session.flush()

    assert "1526" in str(excinfo.value) or "no partition" in str(excinfo.value).lower()


def test_view_hides_extended_session_bars(db_session: Session) -> None:
    """v_price_bars_regular seans disi barlari GIZLER (PB S5.10)."""
    regular = _bar(datetime(2026, 9, 2, 14, 30))
    extended = _bar(datetime(2026, 9, 2, 8, 5))
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

    assert seen == [datetime(2026, 9, 2, 14, 30)]


def test_symbol_column_joins_symbols_despite_missing_fk(db_session: Session) -> None:
    """FK yok ama TIP birebir ayni: JOIN ERROR 3780 vermemeli (PB S5.1).

    FK'yi partition icin feda ettik; bu JOIN'in calismasi, oksuz satir
    denetiminin (PB S7.5/2) hic mumkun olup olmadigini belirler.
    """
    db_session.add(_bar(datetime(2026, 9, 2, 14, 30)))
    db_session.flush()

    orphans = (
        db_session.execute(
            text(
                "SELECT DISTINCT b.symbol FROM price_bars b "
                "LEFT JOIN symbols s ON s.symbol = b.symbol WHERE s.symbol IS NULL"
            )
        )
        .scalars()
        .all()
    )

    assert orphans == ["AAPL"], "FK olmadigi icin oksuz satir YAZILABILIR olmali"
