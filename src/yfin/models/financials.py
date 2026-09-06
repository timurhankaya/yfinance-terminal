"""Finansal tablolar, takvim, earnings_dates ve SEC dosyalamalari (S5.2, S5.3).

Finansal tablolar UZUN (EAV) semadadir: kalem seti sembole ve sektore gore
degisir (10 sembolde 302 farkli etiket olculdu), genis sema her yeni kalemde
migration isterdi.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    desc,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    FactValueType,
    HashType,
    PriceType,
    RawJsonType,
    ShortHashType,
    SymbolType,
    TsType,
)


class StatementKind(enum.StrEnum):
    # PostgreSQL ENUM degerlerini `pg_enum` OID'i olarak saklar ve FK
    # ETIKET uzerinden baglanir: deger sirasi degisse bile FK BOZULMAZ
    # (olculdu: `ALTER TYPE ... ADD VALUE ... BEFORE` sonrasi enumsortorder
    # 1.5 oldu ve bilesik FK'li satir saglam kaldi). MySQL'in ordinal
    # tuzagi YOKTUR; deger araya da eklenebilir.
    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    # get_valuation_measures cercevesi finansal tablolarla AYNI sekildedir
    # (index=kalem etiketi, kolon=donem sonu); ayri bir tablo acmak yerine
    # EAV'nin `statement` boyutuna dorduncu deger olarak girer.
    VALUATION = "valuation"


class StatementFreq(enum.StrEnum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    TTM = "ttm"


def _enum_values(e: type[enum.Enum]) -> list[str]:
    return [m.value for m in e]


# ENUM tanimi TEK kaynaktan gelir ve iki tabloda PAYLASILIR (S5.1).
# Gerekce TEK TANIM YERI ilkesidir: iki ayri Enum() nesnesinin deger
# listeleri sessizce ayrisabilir. Teknik bir cakisma riski YOKTUR --
# SQLAlchemy ayni MetaData icinde ayni adli tipi checkfirst=False ile
# bile tekillestirir (olculdu); yani bu bir BAKIM karari, zorunluluk
# degil.
STATEMENT_ENUM = Enum(
    StatementKind, values_callable=_enum_values, name="statement_kind", native_enum=True
)
FREQ_ENUM = Enum(
    StatementFreq, values_callable=_enum_values, name="statement_freq", native_enum=True
)

# yfinance'in freq parametresi ('yearly'/'quarterly'/'trailing') ile semadaki
# adlar KASITLI olarak farklidir; donusum burada, tek yerde tanimlidir.
API_FREQ: dict[StatementFreq, str] = {
    StatementFreq.ANNUAL: "yearly",
    StatementFreq.QUARTERLY: "quarterly",
    StatementFreq.TTM: "trailing",
}

# Olculen max kalem etiketi 60 karakter (MSFT bilanco); kapali evrenin
# (const.fundamentals_keys, 375 etiket) max'i da 60. VARCHAR'da fazla
# genislik depolama maliyeti uretmez.
ITEM_KEY_LENGTH = 128


class FinancialPeriod(Base):
    """Bir (sembol, tablo, frekans, donem) basligi.

    content_hash degismediginde kalemler yeniden yazilmaz; baslik satiri yine
    de yazilir ve fetched_at 'son dogrulama zamani' olarak ilerler (S6.3/b).
    """

    __tablename__ = "financial_periods"
    __table_args__ = (
        Index("ix_financial_periods_period_end", "period_end"),
    )

    symbol: Mapped[str] = mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=True,
    )
    statement: Mapped[StatementKind] = mapped_column(STATEMENT_ENUM, primary_key=True)
    freq: Mapped[StatementFreq] = mapped_column(FREQ_ENUM, primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    # info.financialCurrency; THYAO.IS tablolari USD, fiyatlari TRY
    currency: Mapped[str | None] = mapped_column(String(8, collation="C"))
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class FinancialFact(Base):
    """Tek bir kalem degeri.

    symbols'a DOGRUDAN FK yoktur: kisit ebeveyn uzerinden gecer ve
    ON DELETE RESTRICT orada zorlanir. Bilesik FK kolonlari PK'nin on eki
    oldugu icin ek indeks gerekmez.
    """

    __tablename__ = "financial_facts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["symbol", "statement", "freq", "period_end"],
            [
                "financial_periods.symbol",
                "financial_periods.statement",
                "financial_periods.freq",
                "financial_periods.period_end",
            ],
            onupdate="CASCADE",
            ondelete="CASCADE",
            name="fk_financial_facts_period",
        ),
        # WHERE period_end=? aksi halde full scan yapar (600k satirda 148 ms)
        Index("ix_financial_facts_period_item", "period_end", "item_key"),
        # "tum sembollerde TotalRevenue"
        Index("ix_financial_facts_item_period", "item_key", "period_end"),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    statement: Mapped[StatementKind] = mapped_column(STATEMENT_ENUM, primary_key=True)
    freq: Mapped[StatementFreq] = mapped_column(FREQ_ENUM, primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    item_key: Mapped[str] = mapped_column(AsciiKeyType(ITEM_KEY_LENGTH), primary_key=True)
    # NaN hucreler hic yazilmadigi icin NOT NULL
    value: Mapped[Decimal] = mapped_column(FactValueType(), nullable=False)


# --- ticker_calendar (snapshot cifti) --------------------------------------


def _calendar_columns() -> list[Column[Any]]:
    return [
        Column("dividend_date", Date, nullable=True),
        Column("ex_dividend_date", Date, nullable=True),
        Column("earnings_date_start", Date, nullable=True),
        Column("earnings_date_end", Date, nullable=True),
        # Ham liste uzunlugu: "tek tarih" ile "aralik" ayrimi korunur
        Column("earnings_date_count", SmallInteger, nullable=False, server_default="0"),
        Column("earnings_high", PriceType(), nullable=True),
        Column("earnings_low", PriceType(), nullable=True),
        Column("earnings_average", PriceType(), nullable=True),
        Column("revenue_high", BigNumType(), nullable=True),
        Column("revenue_low", BigNumType(), nullable=True),
        Column("revenue_average", BigNumType(), nullable=True),
        Column("raw_json", RawJsonType(), nullable=False),
        Column("content_hash", HashType(), nullable=False),
    ]


def _calendar_table(name: str, *, historical: bool) -> Table:
    cols: list[Column[Any]] = [
        Column(
            "symbol",
            SymbolType(),
            ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        )
    ]
    if historical:
        cols.append(Column("fetched_at", TsType(), primary_key=True, nullable=False))
    cols.extend(_calendar_columns())
    if not historical:
        cols.append(Column("fetched_at", TsType(), nullable=False))
    # (symbol, fetched_at DESC) indeksi ACILMAZ: PK'nin ta kendisidir ve
    # PostgreSQL btree indeksi her iki yonde de taranabilir.
    return Table(name, Base.metadata, *cols)


ticker_calendar = _calendar_table("ticker_calendar", historical=False)
ticker_calendar_history = _calendar_table("ticker_calendar_history", historical=True)


class EarningsDate(Base):
    """Gecmis ve gelecek kazanc tarihleri.

    fact_hash PK'ya ZORUNLU olarak girer: AAPL 2002-07-16 16:00 damgasinda
    iki satir var ve tek farklari Surprise(%) (2.55 / 13.43); EPS alanlarinin
    ikisi de NaN. (symbol, ts) PK'si hangi satirin kazanacagini calistirma
    sirasina birakirdi.
    """

    __tablename__ = "earnings_dates"
    __table_args__ = (
        Index("ix_earnings_dates_ts", "earnings_ts_utc"),
    )

    symbol: Mapped[str] = mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=True,
    )
    earnings_ts_utc: Mapped[datetime] = mapped_column(TsType(), primary_key=True)
    fact_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    earnings_date_local: Mapped[date] = mapped_column(Date, nullable=False)
    # Olcumde THYAO.IS, SAP.DE ve 7203.T dahil hepsi America/New_York
    tz_name: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    eps_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    reported_eps: Mapped[Decimal | None] = mapped_column(PriceType())
    surprise_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class SecFiling(Base):
    """SEC dosyalamalari. ABD disinda kaynak {} (dict) doner -> empty."""

    __tablename__ = "sec_filings"
    __table_args__ = (
        Index("ix_sec_filings_symbol_date", "symbol", desc(text("filing_date"))),
        Index("ix_sec_filings_type", "filing_type"),
    )

    symbol: Mapped[str] = mapped_column(
        SymbolType(),
        ForeignKey("symbols.symbol", onupdate="CASCADE", ondelete="RESTRICT"),
        primary_key=True,
    )
    # edgarUrl icindeki accession no (80/80 basarili); bulunamazsa
    # sha256(date|type|title)[:32]
    filing_id: Mapped[str] = mapped_column(AsciiKeyType(64), primary_key=True)
    filing_date: Mapped[date] = mapped_column(Date, nullable=False)
    filed_ts_utc: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    filing_type: Mapped[str] = mapped_column(AsciiKeyType(32), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    edgar_url: Mapped[str | None] = mapped_column(Text)
    # yfinance ek listesini {type: url} sozlugune cevirirken ayni tipteki
    # ikinci egi UZERINE YAZAR; bu sayi tekil ek tipi sayisidir.
    exhibit_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class SecFilingExhibit(Base):
    """Dosyalama ekleri.

    url_hash PK'ya girer: ayni dosyalamada iki farkli URL'li EX-99.1
    gercekte olur ve (symbol, filing_id, exhibit_type) PK'si ikincisini
    tekillik ihlaliyle dusururdu. url TEXT oldugu icin dogrudan PK'ya
    giremez (btree tuple siniri).
    """

    __tablename__ = "sec_filing_exhibits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["symbol", "filing_id"],
            ["sec_filings.symbol", "sec_filings.filing_id"],
            onupdate="CASCADE",
            ondelete="CASCADE",
            name="fk_sec_filing_exhibits_filing",
        ),
    )

    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)
    filing_id: Mapped[str] = mapped_column(AsciiKeyType(64), primary_key=True)
    exhibit_type: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    url_hash: Mapped[str] = mapped_column(ShortHashType(), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "API_FREQ",
    "FREQ_ENUM",
    "ITEM_KEY_LENGTH",
    "STATEMENT_ENUM",
    "EarningsDate",
    "FinancialFact",
    "FinancialPeriod",
    "SecFiling",
    "SecFilingExhibit",
    "StatementFreq",
    "StatementKind",
    "ticker_calendar",
    "ticker_calendar_history",
]
