"""Kesif tablolari: Search, Lookup, Screener (SQ S5).

On tablo. Uc grup:

1. KAPI -- `discovery_asof_state`. `asof_state` KULLANILAMAZ (SQ K3a):
   onun `symbol` kolonu `symbol_fk_column` ile tanimlidir, yani
   `symbols.symbol`'a `ON DELETE RESTRICT` FK tasir. Serbest terim
   (`"Turkish Airlines"`) `symbols`ta YOKTUR ve kapi satiri `ERROR 1452`
   alirdi. SI ayni duvara carpip `domain_asof_state`i acmisti.

2. SEARCH / LOOKUP -- terim kapsamli, as-of. Anahtarlari `query_term`dir,
   `symbol` DEGIL: bir arama teriminin sonucu birden cok sembol tasir.

3. SCREENER -- `screen_runs` (kapi + veri), `screen_members` (cocuk) ve
   `screen_quotes` (EKRANDAN BAGIMSIZ, SQ K5).

Sembol kolonlarinda FK YOKTUR (SQ K9): kesif dataset'leri TANIMI GEREGI
evren disi sembol dondurur. FK olsaydi `symbols` yazimi herhangi bir
nedenle dustugunde o hucrenin TUM verisi rollback olurdu -- `news_symbols`
ile ayni gerekce (models/news.py:45-51). FK olmadigi icin her sembol
kolonunda index ACIKCA tanimlanir (S S5.6).
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.mysql import SMALLINT
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    MYSQL_TABLE_ARGS,
    AsciiKeyType,
    Base,
    HashType,
    PriceType,
    RawJsonType,
    SymbolType,
    TsType,
)
from yfin.models.columns import make_column
from yfin.models.domains import REPORT_ID_LENGTH
from yfin.models.fields import SCREENER_QUOTE_FIELDS

# Serbest arama terimi. Sembol terimleri olculen en fazla 17 karakter
# (SQ S4.1/16); 64 serbest terime pay birakir. CLI bu siniri ASANI
# REDDEDER, kirpmaz: sessiz kirpma iki farkli terimi ayni kapi satirina
# dusurup birinin verisini digerinin sanmasina yol acardi.
QUERY_TERM_LENGTH = 64

# `ALGO_WATCHLIST` seklinde `slug`, `PREDEFINED_SCREENER` seklinde
# `canonicalName` (SQ S4.1/6). Olculen en uzun:
# `most-bought-by-activist-hedge-funds` = 35.
LIST_KEY_LENGTH = 128

# `lookupTotals` DOKUZ tip bildirir; `LOOKUP_TYPES` sabiti sekiz tanir --
# `privateCompany` orada YOKTUR (lookup.py:31). Bu yuzden ENUM DEGIL,
# serbest metin: kaynak yeni bir tip bildirdiginde sema degisikligi
# gerektirmez.
LOOKUP_TYPE_LENGTH = 24

# `sync_run_items.symbol` VARCHAR(32) ascii_bin ve screener tarafinda
# kapsam etiketi olarak ekran anahtarini tasir (SQ S5.13).
SCREEN_KEY_LENGTH = 32


def _query_term_column(**kwargs: object) -> Mapped[str]:
    return mapped_column(AsciiKeyType(QUERY_TERM_LENGTH), **kwargs)  # type: ignore[arg-type]


class ScreenKind(enum.StrEnum):
    PREDEFINED = "predefined"
    CUSTOM = "custom"


class ScreenQuoteType(enum.StrEnum):
    """`yf.screen`in POST govdesine yazdigi `quoteType`.

    Uc deger, uc sorgu sinifi: EquityQuery / FundQuery / ETFQuery.
    """

    EQUITY = "EQUITY"
    MUTUALFUND = "MUTUALFUND"
    ETF = "ETF"


def _enum(kind: type[enum.StrEnum]) -> Enum:
    return Enum(kind, values_callable=lambda e: [m.value for m in e])


# --- 1. kapi ---------------------------------------------------------------


class DiscoveryAsOfState(Base):
    """Search / Lookup kapisi (SQ S5.1).

    `query_term`de FK YOKTUR ve bu tablonun VAROLUS SEBEBI budur (SQ K3a).
    """

    __tablename__ = "discovery_asof_state"
    __table_args__ = (
        Index("ix_discovery_asof_dataset_date", "dataset", "as_of_date"),
        MYSQL_TABLE_ARGS,
    )

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # Son DEGISIMIN as-of gunu
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ilk INSERT'te yazilir, bir daha guncellenmez (AH S5.4)
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Son DOGRULAMA zamani: hash esitse de guncellenir
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# --- 2. search / lookup ----------------------------------------------------


class SearchQuote(Base):
    """`Search.quotes` -- bir terimin dondurdugu semboller (SQ S5.2).

    SEMBOLSUZ satirlar bu tabloya GIRMEZ (SQ K14): `include_cb=True`
    varsayilani Crunchbase ozel-sirket kayitlari dondururor
    (`{index, name, permalink, isYahooFinance}`) ve normalize onlari eler.
    """

    __tablename__ = "search_quotes"
    __table_args__ = (Index("ix_search_quotes_symbol", "symbol"), MYSQL_TABLE_ARGS)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # Yanittaki 0-TABANLI sira (SQ S5.14). Kaynagin kendi siralamasi
    # skorudur; sembolsuz satirlar ELENDIKTEN SONRA numaralandirilir.
    rank_index: Mapped[int] = mapped_column(SMALLINT, nullable=False)
    # Olculen aralik 12,2 - 16.067.500,0
    score: Mapped[Decimal | None] = mapped_column(PriceType())
    quote_type: Mapped[str | None] = mapped_column(String(32))
    type_disp: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(32))
    exch_disp: Mapped[str | None] = mapped_column(String(64))
    short_name: Mapped[str | None] = mapped_column(String(128))
    long_name: Mapped[str | None] = mapped_column(String(255))
    # Sektor/endustri ailesi YALNIZ EQUITY satirlarinda gelir (SQ S4.1/2);
    # eksiklik hata degil, NULL.
    sector: Mapped[str | None] = mapped_column(String(64))
    sector_disp: Mapped[str | None] = mapped_column(String(64))
    industry: Mapped[str | None] = mapped_column(String(128))
    industry_disp: Mapped[str | None] = mapped_column(String(128))
    disp_sec_ind_flag: Mapped[bool | None] = mapped_column(Boolean)
    is_yahoo_finance: Mapped[bool | None] = mapped_column(Boolean)
    prev_name: Mapped[str | None] = mapped_column(String(255))
    name_change_date: Mapped[datetime | None] = mapped_column(TsType())
    # Sembol `symbols` yazimina dahil edilebildi mi (SQ S8.3)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class SearchList(Base):
    """`Search.lists` -- IKI SEKILLI blok (SQ S5.3, S4.1/6).

    `list_type` AYIRICIDIR: `ALGO_WATCHLIST` (12 anahtar, `slug`+`pfId`) ve
    `PREDEFINED_SCREENER` (9 anahtar, `canonicalName`+`total`). Ortak alan
    yalnizca dorttur; ayri tablolar onlari cogaltirdi. Kod tabaninin kendi
    kurali (ozdes olmayan ama akraba sekiller -> tek tablo + ENUM ayirici)
    `institutional_holders`+`mutualfund_holders` deseninden gelir.

    UYELIK SATIRI YOKTUR: blok sembol TASIMAZ. Ama "yalniz sayi var" da
    dogru degildir -- satir uyeligi cozecek KIMLIGI tasir (`pfId`+`userId`
    ya da `canonicalName`). Kapsam disi birakma sebebi MALIYET (ikinci bir
    istek), veri yoklugu degil (SQ S1).
    """

    __tablename__ = "search_lists"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    list_key: Mapped[str] = mapped_column(AsciiKeyType(LIST_KEY_LENGTH), primary_key=True)

    rank_index: Mapped[int] = mapped_column(SMALLINT, nullable=False)
    list_type: Mapped[str | None] = mapped_column(String(32))
    # ALGO_WATCHLIST'te `name`, PREDEFINED_SCREENER'da `title`
    name: Mapped[str | None] = mapped_column(String(255))
    score: Mapped[Decimal | None] = mapped_column(PriceType())
    icon_url: Mapped[str | None] = mapped_column(Text)
    # --- yalniz ALGO_WATCHLIST ---
    brand_slug: Mapped[str | None] = mapped_column(String(64))
    pf_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(64))
    symbol_count: Mapped[int | None] = mapped_column(Integer)
    daily_percent_gain: Mapped[Decimal | None] = mapped_column(PriceType())
    follower_count: Mapped[int | None] = mapped_column(Integer)
    # --- yalniz PREDEFINED_SCREENER ---
    yahoo_id: Mapped[str | None] = mapped_column(String(64))
    total: Mapped[int | None] = mapped_column(Integer)
    is_premium: Mapped[bool | None] = mapped_column(Boolean)

    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class SearchReportHit(Base):
    """Terim <-> rapor bagi (SQ S5.5).

    `domain_report_links`in kardesi. FK BURADA VARDIR (sembol kolonlarinin
    aksine): `report_id` sembol degildir, evren disilik sorunu yoktur ve
    ebeveyn satiri ayni transaction'da, kapili yazimlardan ONCE yazilir
    (SQ S6.2.1).
    """

    __tablename__ = "search_report_hits"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    report_id: Mapped[str] = mapped_column(
        AsciiKeyType(REPORT_ID_LENGTH),
        ForeignKey("research_reports.report_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    rank_index: Mapped[int] = mapped_column(SMALLINT, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class LookupResult(Base):
    """`Lookup` belgeleri (SQ S5.6)."""

    __tablename__ = "lookup_results"
    __table_args__ = (Index("ix_lookup_results_symbol", "symbol"), MYSQL_TABLE_ARGS)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # Yanittaki 0-tabanli sira (SQ S5.14)
    rank_index: Mapped[int] = mapped_column(SMALLINT, nullable=False)
    # Kaynagin KENDI `rank_index` alani -- bir sira DEGIL, Yahoo'nun siralama
    # skoru (olculen ornek 30007). Adi ayrilmasaydi `rank_index` ile karisirdi.
    source_rank: Mapped[int | None] = mapped_column(Integer)
    # Hangi cagridan geldigi. K6 ADAPTIF oldugu icin ZORUNLU: ayni sembol
    # `equity` ve `etf` cagrilarinin ikisinde birden donebiliyor ve PK'da
    # olmadigi icin son yazan kazanir -- hangi cagrinin yazdigi
    # denetlenebilir olmalidir.
    lookup_type: Mapped[str | None] = mapped_column(String(LOOKUP_TYPE_LENGTH))
    quote_type: Mapped[str | None] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(32))
    short_name: Mapped[str | None] = mapped_column(String(128))
    # YALNIZ `equity` belgelerinde dolu (SQ S4.1/9)
    industry_name: Mapped[str | None] = mapped_column(String(128))
    industry_link: Mapped[str | None] = mapped_column(Text)
    fullday_price: Mapped[Decimal | None] = mapped_column(PriceType())
    fullday_change: Mapped[Decimal | None] = mapped_column(PriceType())
    fullday_change_percent: Mapped[Decimal | None] = mapped_column(PriceType())
    regular_market_price: Mapped[Decimal | None] = mapped_column(PriceType())
    regular_market_change: Mapped[Decimal | None] = mapped_column(PriceType())
    regular_market_percent_change: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)


class LookupTotal(Base):
    """`lookupTotals` -- eksiksizlik kanitinin tasiyicisi (SQ S5.7, S9.6/2).

    Ayni yanitta bedava gelir. `total` ile fiili belge sayisinin farki
    KIRPILMAYI belgeler: olculdu, `GOLD` icin `lookupTotals.all` 7.273
    bildirirken `documents` 995 dondu. Bu fark ayni zamanda K6'nin adaptif
    dalini TETIKLEYEN sinyaldir.
    """

    __tablename__ = "lookup_totals"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    query_term: Mapped[str] = _query_term_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    lookup_type: Mapped[str] = mapped_column(String(LOOKUP_TYPE_LENGTH), primary_key=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# --- 3. screener -----------------------------------------------------------


class Screen(Base):
    """Ekranin STATIK kimligi (SQ S5.8) -- `domains` tablosunun kardesi.

    `screens.py`deki `ScreenDef` kumesinden seed edilir. TANIMIN kaynagi o
    dosya, KOSU ANINDAKI ETKINLIGIN kaynagi bu tablonun `is_enabled`
    kolonudur; operator DB'de kapattiginda dosya onu geri acmaz.
    """

    __tablename__ = "screens"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    screen_key: Mapped[str] = mapped_column(AsciiKeyType(SCREEN_KEY_LENGTH), primary_key=True)
    kind: Mapped[ScreenKind] = mapped_column(_enum(ScreenKind), nullable=False)
    quote_type: Mapped[ScreenQuoteType] = mapped_column(_enum(ScreenQuoteType), nullable=False)
    # Predefined'da ILK GET sayfasindan TAZELENIR (SQ S4.1/13); custom'da
    # `ScreenDef`ten gelir ve tazelenmez -- POST yaniti metadata tasimaz.
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # SQ K15: `sortAsc` varsayilani AZALAN; sira acikca tutulmazsa sayfalar
    # arasi tutarsizlik sembol atlatir.
    sort_field: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_asc: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    definition_json: Mapped[str | None] = mapped_column(RawJsonType())
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ScreenRun(Base):
    """Ekranin gunluk basligi -- HEM kapi HEM veri (SQ S5.9).

    `HashGatedDataset`in `financial_periods` deseni: kapi bir VERI
    tablosudur ve `content_hash` degismediginde cocuk (`screen_members`)
    hic yazilmaz.

    `content_hash` YALNIZ KADROYU kapsar (SQ K4). Kotasyon metrikleri
    govdeye girseydi fiyat her gun oynadigi icin hash HICBIR ZAMAN
    esitlenmez ve mekanizma sessizce olurdu.
    """

    __tablename__ = "screen_runs"
    __table_args__ = (Index("ix_screen_runs_date", "as_of_date"), MYSQL_TABLE_ARGS)

    screen_key: Mapped[str] = mapped_column(AsciiKeyType(SCREEN_KEY_LENGTH), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # Yahoo'nun bildirdigi GERCEK eslesme sayisi
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    # Yanittan alinan kotasyon sayisi. `total` ile farki, ekranin sayfa
    # sinirina takildigini SESSIZ DEGIL KAYITLA gosterir (SQ S9.6/1).
    fetched_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    # Kapi sozlesmesi kolonu: `screen_members` satir sayisi
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(SMALLINT, nullable=False)
    yahoo_id: Mapped[str | None] = mapped_column(String(64))
    version_id: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[datetime | None] = mapped_column(TsType())
    criteria_json: Mapped[str | None] = mapped_column(RawJsonType())
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ScreenMember(Base):
    """Ekranin o gunku kadrosu -- kapinin COCUGU (SQ S5.10).

    `replace_scope` kapsami `(screen_key, as_of_date)`: kadro gun icinde
    degistiginde (olculdu, `day_gainers` 122 -> 117) gunun SON kosusu
    kazanir. Duz upsert olsaydi sabah cikip oglen dusen sembol o gunun
    kadrosunda KALICI olarak yanlis gorunurdu.
    """

    __tablename__ = "screen_members"
    __table_args__ = (Index("ix_screen_members_symbol", "symbol"), MYSQL_TABLE_ARGS)

    screen_key: Mapped[str] = mapped_column(AsciiKeyType(SCREEN_KEY_LENGTH), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # `offset + sayfa ici 0-tabanli indeks` = ekranin `sort_field`ina gore
    # MUTLAK sira. Hash govdesindedir: kadro ayni kalip sira degistiginde
    # bu GERCEK bir degisimdir.
    rank_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


# `screen_quotes` -- EKRANDAN BAGIMSIZ (SQ K5).
#
# PK `(symbol, as_of_date)`: bes ekranda birden gorunen sembolun 102 alani
# bes kez yazilmaz. Kapinin SILME KAPSAMINA GIRMEZ ve bu ZORUNLUDUR --
# bir sembolun kotasyonu tek bir ekranin mali degildir; kadrodan cikmasi
# kotasyonunu silmez.
#
# Kolonlarin 75'i `INFO_FIELDS` ile ayni kaynak anahtarindan uretilir, yani
# `ticker_info` ile AYNI kolon adlarini tasir ve iki tablo JOIN'siz
# karsilastirilabilir (SQ S4.5).
screen_quotes = Table(
    "screen_quotes",
    Base.metadata,
    Column("symbol", SymbolType(), primary_key=True, nullable=False),
    Column("as_of_date", Date, primary_key=True, nullable=False),
    *(make_column(f) for f in SCREENER_QUOTE_FIELDS),
    Column("is_known", Boolean, nullable=False, server_default="0"),
    Column("fetched_at", TsType(), nullable=False),
    # `corporateActions` LISTEDIR ve kolona cikmaz; burada kalir.
    Column("raw_json", RawJsonType(), nullable=False),
    **MYSQL_TABLE_ARGS,  # type: ignore[arg-type]
)
