"""Tip katmani invaryantlari (PG S2). Veritabanina DOKUNMAZ.

Bu testler tek tek tablolara degil, tip fabrikalarina ve
`Base.metadata`'nin TAMAMINA bakar. Gerekce: bir politikanin tek bir
yardimcida tanimli olmasi onu KORUMAZ -- yeni bir tablo yardimciyi
kullanmadan da yazilabilir ve sessizce sapabilir.
"""

from __future__ import annotations

from sqlalchemy import Enum, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from yfin.models import Base
from yfin.models.base import NAMING_CONVENTION, RawJsonType, TsType


def test_timestamps_are_timestamptz_with_microseconds() -> None:
    """DATETIME(6) -> TIMESTAMP(6) WITH TIME ZONE.

    Generic `sqlalchemy.TIMESTAMP` `precision` KABUL ETMEZ (TypeError);
    dialect tipi zorunludur. Alti hane sarttir: saniye hassasiyeti
    ticker_info_history PK'sinda (symbol, fetched_at) cakisma uretirdi ve
    PostgreSQL kesirleri YUVARLAR, kesmez.
    """
    ts = TsType()
    assert isinstance(ts, TIMESTAMP)
    assert ts.timezone is True
    assert ts.precision == 6


def test_raw_json_is_plain_text() -> None:
    """JSONB DEGIL: anahtar sirasini degistirir, NaN'i reddeder,
    sayilari normalize eder -- ucu de content_hash'i bozar."""
    assert isinstance(RawJsonType(), Text)


def test_metadata_has_naming_convention() -> None:
    """Adsiz kisitlarda Alembic KARARSIZ ad uretir ve `yfin db revision`
    her cagrildiginda sahte fark raporlar -- yani "bos diff" kapisi HIC
    acilmaz (PG S2.6)."""
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert "ck" in NAMING_CONVENTION


def test_no_string_column_lacks_c_collation() -> None:
    """Her uzunluklu String/VARCHAR kolonu COLLATE "C" tasimali.

    Duz `String(n)` kolonlari MySQL'de TABLO varsayilanini
    (utf8mb4_0900_ai_ci) aliyordu; PostgreSQL'de VERITABANI varsayilani
    (en_US.utf8) uygulanir -- yani ne "C" ne de eski davranis.

    UC KAYNAK vardir ve UCU DE kapsanmalidir:
      1. models/base.py fabrikalari (SymbolType, AsciiKeyType, ...)
      2. models/kinds.py `strN` kind'lari (_c_string)
      3. model dosyalarinda DOGRUDAN yazilmis `String(n)` cagrilari
    Ucuncusu ilk taramada gozden kacmisti; bu testin varlik sebebi tam
    olarak budur (PG S2.5).

    `Enum` HARIC TUTULUR: SQLAlchemy'de `Enum` `String`den turer ama
    PostgreSQL'de ayri bir tiptir (CREATE TYPE) ve collation almaz.
    """
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, String)
        and not isinstance(col.type, Enum)
        and col.type.length is not None
        and getattr(col.type, "collation", None) != "C"
    ]
    assert not offenders, f"collation'siz VARCHAR ({len(offenders)}): {offenders}"


def test_row_size_budget_helpers_are_gone() -> None:
    """PostgreSQL'de 65 535 baytlik satir siniri YOKTUR: genis degerler
    TOAST'a tasinir. Pratik sinir kolon sayisidir (1 600)."""
    import yfin.models.columns as columns

    assert not hasattr(columns, "MYSQL_ROW_SIZE_LIMIT")
    assert not hasattr(columns, "estimated_row_size")


def test_mysql_table_args_is_gone() -> None:
    import yfin.models.base as base

    assert not hasattr(base, "MYSQL_TABLE_ARGS")
