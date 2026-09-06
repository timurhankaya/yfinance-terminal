"""INSERT_CHUNK: buyuk TableWrite'larin dilimlenmesi (PB S6.7).

MySQL'e DOKUNMAZ: Session yerine cagrilari kaydeden bir sahte kullanilir,
boylece "kac INSERT uretildi" ve "her dilim ayni kolon setini tasiyor mu"
sorulari agsiz ve DB'siz cevaplanir.

Dilimlemenin tek gercek tuzagi align_rows SIRASIDIR: kolon seti satirdan
satira degisebilir (fon olmayan sembolde 'Capital Gains' yok) ve
align_rows dilimlemeden SONRA uygulanirsa her dilim FARKLI bir kolon
setiyle ve farkli bir ON DUPLICATE KEY UPDATE haritasiyla yazilir.
"""

from __future__ import annotations

from typing import Any

import pytest

from yfin.datasets.base import TableWrite
from yfin.persistence import INSERT_CHUNK, MySQLRowWriter


class RecordingSession:
    """execute() cagrilarini kaydeden sahte Session."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return _FakeResult()


class _FakeResult:
    def scalar_one(self) -> int:
        return 0


def _write(rows: list[dict[str, Any]]) -> TableWrite:
    return TableWrite(
        table="price_bars",
        rows=rows,
        key_columns=("symbol", "bar_interval", "ts_utc"),
        update_columns=("close", "volume"),
    )


def _bar_rows(count: int, *, drop_volume_after: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(count):
        row: dict[str, Any] = {
            "symbol": "AAPL",
            "bar_interval": "1m",
            "ts_utc": f"2026-09-02 14:{i:02d}:00",
            "local_date": "2026-09-02",
            "close": 100 + i,
        }
        if drop_volume_after is None or i < drop_volume_after:
            row["volume"] = 1000 + i
        rows.append(row)
    return rows


def _insert_statements(session: RecordingSession) -> list[Any]:
    """SELECT dogrulama sorgularini eleyip yalniz INSERT'leri dondurur."""
    return [s for s in session.statements if s.__class__.__name__ == "Insert"]


def test_large_write_is_split_into_chunks() -> None:
    session = RecordingSession()
    writer = MySQLRowWriter(session)  # type: ignore[arg-type]

    rows = _bar_rows(INSERT_CHUNK * 2 + 1)
    writer.write(_write(rows))

    inserts = _insert_statements(session)
    assert len(inserts) == 3, f"3 dilim beklenirdi, {len(inserts)} INSERT uretildi"
    counts = [len(stmt.compile().params) // len(rows[0]) for stmt in inserts]
    assert sum(counts) == len(rows)


def test_small_write_produces_single_insert() -> None:
    session = RecordingSession()
    writer = MySQLRowWriter(session)  # type: ignore[arg-type]

    writer.write(_write(_bar_rows(3)))

    assert len(_insert_statements(session)) == 1


def test_every_chunk_carries_the_same_column_set() -> None:
    """align_rows dilimlemeden ONCE uygulanmali.

    Ilk yarida `volume` var, ikinci yarida yok. align_rows once
    uygulanirsa her iki dilim de `volume` tasir (ikincide None); sonra
    uygulanirsa ikinci dilim onu hic gormez ve ON DUPLICATE KEY UPDATE
    kapsamindan sessizce duser.
    """
    session = RecordingSession()
    writer = MySQLRowWriter(session)  # type: ignore[arg-type]

    rows = _bar_rows(INSERT_CHUNK + 2, drop_volume_after=INSERT_CHUNK)
    writer.write(_write(rows))

    inserts = _insert_statements(session)
    assert len(inserts) == 2

    # Her dilimin GERCEKTEN yazdigi kolonlar: compile edilmis parametre
    # adlari "close_m0", "volume_m1" bicimindedir; son alt tireden onceki
    # parca kolon adidir.
    def written_columns(stmt: Any) -> set[str]:
        return {name.rsplit("_m", 1)[0] for name in stmt.compile().params}

    assert written_columns(inserts[0]) == written_columns(inserts[1])
    assert "volume" in written_columns(inserts[1]), (
        "ikinci dilim volume'u hic gormedi: align_rows dilimlemeden SONRA uygulanmis"
    )
    # ve update kapsaminda da kalmali
    for stmt in inserts:
        assert "volume" in set(stmt._post_values_clause.update.keys())


def test_empty_write_produces_no_insert() -> None:
    session = RecordingSession()
    writer = MySQLRowWriter(session)  # type: ignore[arg-type]

    assert writer.write(_write([])) == 0
    assert _insert_statements(session) == []


@pytest.mark.parametrize("size", [1, INSERT_CHUNK - 1, INSERT_CHUNK, INSERT_CHUNK + 1])
def test_chunk_boundaries(size: int) -> None:
    session = RecordingSession()
    writer = MySQLRowWriter(session)  # type: ignore[arg-type]

    writer.write(_write(_bar_rows(size)))

    expected = (size + INSERT_CHUNK - 1) // INSERT_CHUNK
    assert len(_insert_statements(session)) == expected
