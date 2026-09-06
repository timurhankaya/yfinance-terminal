"""Fiyat ve kurumsal islem serileri (S5.2)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    MYSQL_TABLE_ARGS,
    Base,
    PriceType,
    TsType,
    symbol_fk_column,
)


class PriceHistory(Base):
    """interval='1d'. PK borsanin YEREL seans tarihidir."""

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_session_date", "session_date"), MYSQL_TABLE_ARGS)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(primary_key=True)
    # ts_utc ham gercegi korur: pozitif ofsetli borsalarda UTC'ye cevirmek
    # tarihi bir gun geri kaydirir (S5.4)
    ts_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)

    open: Mapped[Decimal | None] = mapped_column(PriceType())
    high: Mapped[Decimal | None] = mapped_column(PriceType())
    low: Mapped[Decimal | None] = mapped_column(PriceType())
    close: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)
    # auto_adjust=False ile gelen ayri kolon
    adj_close: Mapped[Decimal | None] = mapped_column(PriceType())
    volume: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))

    # Bu uc kolon TUREV bilgidir, otorite degildir; tekil dogruluk kaynagi
    # dividends/splits/capital_gains tablolaridir ve v_actions yalniz onlari okur.
    dividend: Mapped[Decimal] = mapped_column(PriceType(), nullable=False, server_default="0")
    split_ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False, server_default="0")
    capital_gain: Mapped[Decimal] = mapped_column(PriceType(), nullable=False, server_default="0")

    # yfinance history(repair=True) ciktisindaki "Repaired?" kolonu (P6.3).
    # MONOTONIKTIR: yalnizca 0 -> 1 yonunde ilerler. Onarim heuristikleri
    # pencere uzunluguna bagli oldugu icin dar bir artimli pencerede ayni
    # satir bir kez 1, ertesi kez 0 gelebilir; duz upsert bunu geri yazar
    # ve kolonun denetim degeri sifirlanirdi.
    is_repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")


class Dividend(Base):
    __tablename__ = "dividends"
    __table_args__ = (Index("ix_dividends_ex_date", "ex_date"), MYSQL_TABLE_ARGS)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    ex_date: Mapped[date] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)


class Split(Base):
    __tablename__ = "splits"
    __table_args__ = (Index("ix_splits_split_date", "split_date"), MYSQL_TABLE_ARGS)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    split_date: Mapped[date] = mapped_column(primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)


class CapitalGain(Base):
    __tablename__ = "capital_gains"
    __table_args__ = (Index("ix_capital_gains_gain_date", "gain_date"), MYSQL_TABLE_ARGS)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    gain_date: Mapped[date] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(PriceType(), nullable=False)


class SharesFull(Base):
    """PK'ya 'shares' DAHILDIR (S5.2).

    Kaynakta ayni tarihte farkli degerler geliyor (AAPL'de 17 tarih).
    (symbol, as_of_date) ikilisi olsaydi hangi degerin kazanacagi
    calistirma sirasina bagli olurdu.
    """

    __tablename__ = "shares_full"
    __table_args__ = (Index("ix_shares_full_as_of_date", "as_of_date"), MYSQL_TABLE_ARGS)

    symbol: Mapped[str] = symbol_fk_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(primary_key=True)
    shares: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True)
    ts_utc: Mapped[datetime | None] = mapped_column(TsType())
