"""as-of kapi tablosu (AH S5.4).

Bu tablo VERI tasimaz; 13 as-of dataset'inin "en son ne zaman degisti / ne
zaman dogrulandi" kaydidir. Hash'in veri tablolarinda degil burada durmasinin
nedeni: `funds_data` dort tabloya yaziyor; hash'i her satira kopyalamak dort
ayri dogruluk kaynagi yaratir ve iki soruyu birden cevaplanamaz kilardi.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    MYSQL_TABLE_ARGS,
    AsciiKeyType,
    Base,
    HashType,
    TsType,
    symbol_fk_column,
)


class AsOfState(Base):
    __tablename__ = "asof_state"
    __table_args__ = (
        Index("ix_asof_state_dataset_date", "dataset", "as_of_date"),
        MYSQL_TABLE_ARGS,
    )

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # Son DEGISIMIN as-of gunu
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    # Denetim icin; kapi kararinda KULLANILMAZ (karar yalnizca hash'e bakar)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ilk INSERT'te yazilir, bir daha guncellenmez: update_columns kapsaminin
    # DISINDADIR, aksi halde ON DUPLICATE KEY UPDATE bu kurali bozardi.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Son DOGRULAMA zamani: hash esitse de guncellenir (hash_gated.py'nin
    # kurdugu ilke). Sapilsaydi "bu sembol en son ne zaman kontrol edildi"
    # sorusu cevapsiz kalirdi.
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
