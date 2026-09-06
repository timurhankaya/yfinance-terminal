"""Piyasa-kapsamli tablolar: market_status/summary ve takvimler (S5.4).

Hicbirinde sembol FK'si YOKTUR: kaynakta evren disi semboller geliyor
(splits takviminde Kore hisseleri, earnings takviminde Oracle). FK olsaydi
tek bir yabanci sembol tum turun transaction'ini dusururdu; news_symbols ile
ayni gerekce. is_known bayragi sembolun evrende olup olmadigini isaretler.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    HashType,
    KeyTextType,
    PriceType,
    RawJsonType,
    RegionType,
    SymbolType,
    TsType,
)

# Yahoo market_cap alani float doner (443707733500.90027)
MARKET_CAP_TYPE = Numeric(38, 4, asdecimal=True)


# --- market_status / market_summary (snapshot ciftleri) --------------------


def _status_columns() -> list[Column[Any]]:
    return [
        Column("market_id", String(32, collation="C"), nullable=True),
        Column("name", String(64, collation="C"), nullable=True),
        Column("status", String(32, collation="C"), nullable=True),
        Column("yfit_market_status", String(64, collation="C"), nullable=True),
        Column("message", Text, nullable=True),
        Column("open_ts_utc", TsType(), nullable=True),
        Column("close_ts_utc", TsType(), nullable=True),
        Column("timezone_name", String(64, collation="C"), nullable=True),
        Column("gmt_offset", Integer, nullable=True),
        Column("tz_short", String(16, collation="C"), nullable=True),
        Column("raw_json", RawJsonType(), nullable=False),
        Column("content_hash", HashType(), nullable=False),
    ]


def _summary_columns() -> list[Column[Any]]:
    return [
        # Board sembolleri (ES=F, ^GSPC) evrende olmayabilir -> FK YOK
        Column("symbol", SymbolType(), nullable=True),
        Column("is_known", Boolean, nullable=False, server_default=text("false")),
        Column("short_name", String(64, collation="C"), nullable=True),
        Column("quote_type", String(32, collation="C"), nullable=True),
        Column("exchange", String(32, collation="C"), nullable=True),
        Column("market_state", String(16, collation="C"), nullable=True),
        Column("currency", String(8, collation="C"), nullable=True),
        Column("regular_market_price", PriceType(), nullable=True),
        Column("regular_market_change", PriceType(), nullable=True),
        Column("regular_market_change_percent", PriceType(), nullable=True),
        Column("regular_market_previous_close", PriceType(), nullable=True),
        Column("regular_market_ts_utc", TsType(), nullable=True),
        Column("exchange_timezone_name", String(64, collation="C"), nullable=True),
        Column("raw_json", RawJsonType(), nullable=False),
        Column("content_hash", HashType(), nullable=False),
    ]


def _region_table(
    name: str, columns: list[Column[Any]], *, historical: bool, with_board: bool
) -> Table:
    cols: list[Column[Any]] = [Column("region", RegionType(), primary_key=True, nullable=False)]
    if with_board:
        cols.append(Column("board_code", AsciiKeyType(8), primary_key=True, nullable=False))
    if historical:
        cols.append(Column("fetched_at", TsType(), primary_key=True, nullable=False))
    cols.extend(columns)
    if not historical:
        cols.append(Column("fetched_at", TsType(), nullable=False))
    args: list[object] = list(cols)
    if with_board:
        # symbol PK'da degil ve FK yok -> InnoDB kendiliginden indeks acmaz
        args.append(Index(f"ix_{name}_symbol", "symbol"))
    return Table(name, Base.metadata, *args)  # type: ignore[arg-type]


market_status = _region_table(
    "market_status", _status_columns(), historical=False, with_board=False
)
market_status_history = _region_table(
    "market_status_history", _status_columns(), historical=True, with_board=False
)
market_summary = _region_table(
    "market_summary", _summary_columns(), historical=False, with_board=True
)
market_summary_history = _region_table(
    "market_summary_history", _summary_columns(), historical=True, with_board=True
)


# --- takvimler -------------------------------------------------------------


class CalendarEarnings(Base):
    __tablename__ = "calendar_earnings"
    __table_args__ = (
        Index("ix_calendar_earnings_start", "event_start_ts_utc"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    event_start_ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    company: Mapped[str | None] = mapped_column(String(255, collation="C"))
    market_cap: Mapped[Decimal | None] = mapped_column(MARKET_CAP_TYPE)
    event_name: Mapped[str | None] = mapped_column(KeyTextType(128))
    timing: Mapped[str | None] = mapped_column(String(8, collation="C"))
    eps_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    reported_eps: Mapped[Decimal | None] = mapped_column(PriceType())
    surprise_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class CalendarEconomic(Base):
    """PK (region, event_time_utc, event_name).

    Index (Event) tekil DEGIL (100 satirda 29 tekrar); ucul anahtar 100/100
    tekil olculdu. event_name KeyTextType'tir: varsayilan collation
    'Enflasyon' ile 'Enflasyon' arasindaki aksani ve harf buyuklugunu yok
    sayip iki farkli olayi tek satira indirirdi.
    """

    __tablename__ = "calendar_economic"
    __table_args__ = (
        Index("ix_calendar_economic_time", "event_time_utc"),
    )

    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    event_time_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    event_name: Mapped[str] = mapped_column(KeyTextType(64), primary_key=True)
    period_for: Mapped[str | None] = mapped_column(String(16, collation="C"))
    actual: Mapped[Decimal | None] = mapped_column(PriceType())
    expected: Mapped[Decimal | None] = mapped_column(PriceType())
    # 'last_value' MySQL 8 REZERVE kelimesidir (ERROR 1064)
    last_reported: Mapped[Decimal | None] = mapped_column(PriceType())
    revised: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class CalendarIpo(Base):
    __tablename__ = "calendar_ipo"
    __table_args__ = (
        Index("ix_calendar_ipo_date", "ipo_date_utc"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    ipo_date_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    action: Mapped[str] = mapped_column(AsciiKeyType(16), primary_key=True)
    company: Mapped[str | None] = mapped_column(String(255, collation="C"))
    exchange: Mapped[str | None] = mapped_column(String(32, collation="C"))
    filing_date: Mapped[date | None] = mapped_column(Date)
    amended_date: Mapped[date | None] = mapped_column(Date)
    price_from: Mapped[Decimal | None] = mapped_column(PriceType())
    price_to: Mapped[Decimal | None] = mapped_column(PriceType())
    price: Mapped[Decimal | None] = mapped_column(PriceType())
    currency: Mapped[str | None] = mapped_column(String(8, collation="C"))
    shares: Mapped[Decimal | None] = mapped_column(BigNumType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class CalendarSplits(Base):
    __tablename__ = "calendar_splits"
    __table_args__ = (
        Index("ix_calendar_splits_payable", "payable_on_utc"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    payable_on_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    company: Mapped[str | None] = mapped_column(String(255, collation="C"))
    optionable: Mapped[bool | None] = mapped_column(Boolean)
    old_share_worth: Mapped[int | None] = mapped_column(Integer)
    share_worth: Mapped[int | None] = mapped_column(Integer)
    # share_worth / old_share_worth; payda 0 veya NULL ise NULL
    ratio: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# Budama icin takvim tablolarinin ZAMAN kolonlari (S7.4). Tablo basina tek
# kolon; elle liste yerine burada tutulur cunku kolon adlari tabloya ozgudur.
CALENDAR_TIME_COLUMNS: dict[str, str] = {
    "calendar_earnings": "event_start_ts_utc",
    "calendar_economic": "event_time_utc",
    "calendar_ipo": "ipo_date_utc",
    "calendar_splits": "payable_on_utc",
}


__all__ = [
    "CALENDAR_TIME_COLUMNS",
    "CalendarEarnings",
    "CalendarEconomic",
    "CalendarIpo",
    "CalendarSplits",
    "market_status",
    "market_status_history",
    "market_summary",
    "market_summary_history",
]
