"""symbols tablosu (S5.2) - altyapi dataset'i, her calistirmada ilk kosar."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import Base, SymbolType, TsType


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        Index("ix_symbols_exchange", "exchange"),
        Index("ix_symbols_isin", "isin"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # isin UNIQUE DEGILDIR: ayni ISIN farkli borsalarda listelenebilir (S5.2)
    isin: Mapped[str | None] = mapped_column(String(16, collation="C"))
    quote_type: Mapped[str | None] = mapped_column(String(32, collation="C"))
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    full_exchange_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    currency: Mapped[str | None] = mapped_column(String(32, collation="C"))
    timezone: Mapped[str | None] = mapped_column(String(64, collation="C"))
    short_name: Mapped[str | None] = mapped_column(String(128, collation="C"))
    long_name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    first_trade_date: Mapped[datetime | None] = mapped_column(TsType())

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # SQ S5.12: sembolu evrene KIM soktu. Kesif yollari (`search`, `lookup`,
    # `screener`) yeni sembolu `is_active=0` ile yazar; aktiflestirme ELLE
    # yapilir (`yfin symbols activate --discovered-by ...`).
    #
    # DIKKAT -- bu kolon ve `discovered_at`, kesif yazimlarinin
    # `update_columns` KAPSAMI DISINDADIR (SQ K10), `is_active` ve
    # `unknown_streak` ile birlikte. Kapsama girselerdi operatorun elle
    # aktiflestirdigi bir sembol, ertesi gun ayni ekranda yeniden
    # gorulduğunde SESSIZCE `is_active=0`a doner ve `yfin sync` onu
    # cekmeyi birakirdi. `first_seen_at`in AH S5.4'te kurdugu "yalniz
    # INSERT'te yazilir" kuralinin aynisi.
    discovered_by: Mapped[str] = mapped_column(
        String(16, collation="C"), nullable=False, server_default="manual"
    )
    discovered_at: Mapped[datetime | None] = mapped_column(TsType())
    # S8.8: ardisik unknown_symbol sayaci; esik asilinca is_active=0
    unknown_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(TsType())

    # `func.now()`, `func.now(6)` DEGIL: PostgreSQL'de `now()` arguman
    # ALMAZ ve `now(6)` "function now(integer) does not exist" verir
    # (olculdu, migration uygulanirken). Hassasiyet KOLON tipinden gelir
    # (TsType = TIMESTAMP(6) WITH TIME ZONE), fonksiyondan degil.
    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(),
        nullable=False,
        server_default=func.now(),
        # `server_onupdate` PostgreSQL'de DDL uretmez (ON UPDATE kolon
        # cumlecigi yoktur, PG S2.11); yalnizca SQLAlchemy'ye degerin
        # sunucu tarafindan degisebilecegini bildirir.
        server_onupdate=func.now(),
    )
