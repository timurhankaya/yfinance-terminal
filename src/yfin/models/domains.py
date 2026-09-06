"""Sektor / endustri (domain) tablolari (SI S5).

BIRLESIK TAKSONOMI, olcumle: sektor ve endustri `overview` kolon setleri
ozdes, `topCompanies` kolon setleri BIREBIR ayni olculdu. Kod tabaninin
kendi kurali -- ozdes kolon seti -> tek tablo + ayirici ENUM
(`institutional_holders`+`mutualfund_holders`, `earnings_estimate`+
`revenue_estimate`) -- burada da uygulanir.

`domain_type` PK'DA DEGILDIR: 11 sektor ve 145 endustri anahtari olcumle
ayrik (kesisim = bos), sembolleri de ayrik. PK'ya konsaydi `parent_key`
self-FK'si iki kolonlu olur ve her JOIN'e tasinirdi.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import BIGINT, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    AsciiKeyType,
    Base,
    BigNumType,
    HashType,
    PriceType,
    RawJsonType,
    RegionType,
    SymbolType,
    TsType,
    symbol_fk_column,
)

# `domain_key` olculen max 37 (`utilities-independent-power-producers`);
# 48 ile pay birakilir.
DOMAIN_KEY_LENGTH = 48
# `report_id` olculen max 50.
REPORT_ID_LENGTH = 64


class DomainType(enum.StrEnum):
    SECTOR = "sector"
    INDUSTRY = "industry"


class FundType(enum.StrEnum):
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"


class RankType(enum.StrEnum):
    PERFORMING = "performing"
    GROWTH = "growth"


DOMAIN_TYPE_ENUM = Enum(
    DomainType,
    values_callable=lambda e: [m.value for m in e],
    name="domain_type",
    native_enum=True,
)
FUND_TYPE_ENUM = Enum(
    FundType,
    values_callable=lambda e: [m.value for m in e],
    name="domain_fund_type",
    native_enum=True,
)
RANK_TYPE_ENUM = Enum(
    RankType,
    values_callable=lambda e: [m.value for m in e],
    name="domain_rank_type",
    native_enum=True,
)


def domain_key_column(**kwargs: object) -> Mapped[str]:
    """`domains.domain_key`'e FK tasiyan anahtar kolonu.

    `ascii_bin`: `TECHNOLOGY` canlida 404 verdi, anahtarlar buyuk/kucuk harf
    DUYARLIDIR; `ascii_general_ci` iki anahtari tek satira indirirdi.
    """
    return mapped_column(
        AsciiKeyType(DOMAIN_KEY_LENGTH),
        ForeignKey("domains.domain_key", onupdate="CASCADE", ondelete="RESTRICT"),
        **kwargs,  # type: ignore[arg-type]
    )


class Domain(Base):
    """Statik kimlik: ad, aciklama, sembol, ebeveyn. As-of DEGIL -- upsert.

    Bu alanlar yilda birkac kez degisir; gunluk anlik goruntu almanin
    karsiligi yoktur.
    """

    __tablename__ = "domains"
    __table_args__ = (
        # Kod tabanindaki ILK CheckConstraint (MySQL 8.0.16+ destekliyor,
        # 8.3.0'da dogrulandi). Endustrinin ebeveyni olmak ZORUNDADIR;
        # sektorde NULL'dir.
        CheckConstraint(
            "domain_type = 'sector' OR parent_key IS NOT NULL",
            name="ck_domains_parent",
        ),
        Index("ix_domains_type_parent", "domain_type", "parent_key"),
    )

    domain_key: Mapped[str] = mapped_column(AsciiKeyType(DOMAIN_KEY_LENGTH), primary_key=True)
    domain_type: Mapped[DomainType] = mapped_column(DOMAIN_TYPE_ENUM, nullable=False)
    # UNIQUE: `^YH311` tek bir domain'e aittir. FK -> symbols: 156 satir
    # `domain_taxonomy` tarafindan yazilir (SI S5.8).
    symbol: Mapped[str] = symbol_fk_column(nullable=False, unique=True)
    # Self-FK. ON DELETE RESTRICT burada da gecerlidir: bir sektoru silmek
    # 145 endustriyi oksuz birakamaz.
    #
    # ON UPDATE **RESTRICT**, CASCADE DEGIL -- ve bu, projenin
    # `symbol_fk_column` deseninden BILINCLI bir sapmadir. MySQL 8, CHECK
    # constraint'te gecen bir kolonda referential ACTION'a izin vermez:
    #
    #   ERROR 3823: Column 'parent_key' cannot be used in a check
    #   constraint 'ck_domains_parent': needed in a foreign key
    #   constraint 'domains_ibfk_2' referential action.
    #
    # Olculdu (MySQL 8.3): CASCADE -> hata; RESTRICT, NO ACTION ve
    # action'siz tanim -> gecerli. Yani secim "CHECK mi, CASCADE mi"
    # ikilisidir. CHECK korunur cunku endustrinin ebeveynsiz olamayacagini
    # DB SEVIYESINDE garanti eder; `domain_key` ise Yahoo'nun sabit
    # slug'idir ('technology', 'software-infrastructure') ve yeniden
    # adlandirilmasi beklenmez. Beklenmedik bir sekilde denenirse RESTRICT
    # GORUNUR bir hata verir, sessiz bir bozulma degil.
    parent_key: Mapped[str | None] = mapped_column(
        AsciiKeyType(DOMAIN_KEY_LENGTH),
        ForeignKey("domains.domain_key", onupdate="RESTRICT", ondelete="RESTRICT"),
    )
    # Olculen max 40
    name: Mapped[str] = mapped_column(String(64, collation="C"), nullable=False)
    # Olculen max 446, 156/156 dolu. SEKTORDE bootstrap yazar; ENDUSTRIDE
    # `industries[]` blogu bu alani icermez, bu yuzden `industry_profile`
    # yazar (SI S7.3) -- ilk profil kosusuna kadar NULL.
    description: Mapped[str | None] = mapped_column(Text)
    # Olculen max 15
    message_board_id: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # Ilk INSERT'te yazilir, bir daha guncellenmez (AH S5.4 kurali):
    # `update_columns` kapsaminin DISINDADIR.
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    # Son DOGRULAMA zamani
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainMetric(Base):
    """as-of, BOLGESIZ: `overview` + `performance` + benchmark tek satirda.

    Bolge kolonu YOKTUR: bu bes blok US/GB/DE/JP/TR'de BIREBIR AYNI olculdu.
    """

    __tablename__ = "domain_metrics"
    __table_args__ = (
        Index("ix_domain_metrics_date", "as_of_date"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # Olculen max 1517 (financial-services)
    companies_count: Mapped[int | None] = mapped_column(Integer)
    # ENDUSTRI `overview`'unda bu anahtar HAM JSON'DA HIC YOKTUR (145/145);
    # yfinance'in `.get()` cagrisi onu None'a ceviriyor. -> nullable
    industries_count: Mapped[int | None] = mapped_column(Integer)
    # Ham `int`; 1,23e8 ... 2,88e13
    market_cap: Mapped[Decimal | None] = mapped_column(BigNumType())
    # 1,32e-5 ... 0,746
    market_weight: Mapped[Decimal | None] = mapped_column(PriceType())
    # 18 ... 11 895 040
    employee_count: Mapped[int | None] = mapped_column(BIGINT(unsigned=True))

    # `performance` blogu -- yfinance HICBIR property ile acmiyor (SI S4.3)
    ytd_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    reg_market_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    one_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    three_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    five_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())

    # `performanceOverviewBenchmark` blogu -- o da yfinance'te YOK
    benchmark_name: Mapped[str | None] = mapped_column(String(64, collation="C"))
    benchmark_ytd_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_reg_market_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_one_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_three_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    benchmark_five_year_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())

    # Yanitin LISTE-DISI kismi: key, name, symbol, sectorKey, sectorName,
    # overview, performance, performanceOverviewBenchmark. Liste bloklari
    # DAHIL DEGILDIR (SI S2): `canonical_json` liste SIRASINI korur ve tam
    # zarf saklansaydi `topCompanies` sirasi (11 sektorun 8'inde 15 dk'da
    # degisti) kapiyi HER KOSUDA acardi -- as-of mekanizmasi sessizce hic
    # calismazdi.
    raw_json: Mapped[str] = mapped_column(RawJsonType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopCompany(Base):
    """Domain'in en buyuk sirketleri. IKI dataset yazar (sektor + endustri).

    Sira (`position`) SAKLANMAZ: siralama olcutu `market_weight` zaten
    kolonda ve sira ondan turetilebilir; saklansaydi sira degisimi
    (11 sektorun 8'inde 15 dakikada) hash'i her kosuda degistirirdi.
    """

    __tablename__ = "domain_top_companies"
    __table_args__ = (
        # PK'nin 4. kolonu oldugu icin "bu sirket hangi sektorlerin ilk
        # 50'sinde" sorgusu aksi halde TAM TARAMA yapardi.
        Index("ix_domain_top_companies_symbol", "symbol"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # FK YOKTUR (SI S2): SGE.L, 285A.T, ODINE.IS evren disi. FK olsaydi tek
    # yabanci sembol TURUN transaction'ini dusururdu (`news_symbols`
    # gerekcesi). Bunun yerine `is_known` bayragi DB'den doldurulur.
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # Olculen: Strong Buy / Buy / Hold / Underperform / Sell. ENUM DEGIL --
    # kapali liste oldugunun kaniti yok (AH'nin `action` karari).
    rating: Mapped[str | None] = mapped_column(String(32, collation="C"))
    market_weight: Mapped[Decimal | None] = mapped_column(PriceType())
    market_cap: Mapped[Decimal | None] = mapped_column(BigNumType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    reg_market_change_pct: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopFund(Base):
    """topETFs + topMutualFunds -- YALNIZ SEKTOR.

    Endustri yaniti bu iki blogu HIC ICERMEZ (top-level anahtar listesiyle
    dogrulandi); sessiz veri kaybi degil, olculmus bir yokluk.

    `fund_type` PK'dadir: kolon setleri birebir ayni, sembol kumeleri bugun
    ayrik -- ama bunun yarin da ayrik kalacaginin kaniti yok.
    """

    __tablename__ = "domain_top_funds"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fund_type: Mapped[FundType] = mapped_column(FUND_TYPE_ENUM, primary_key=True)
    # Ticker OLMAYABILIR: `0P0001WO1I` bir Morningstar kimligi (healthcare
    # topMutualFunds'ta olculdu).
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    # Gunluk 220 fon satirinin 7'sinde YOK; yedisi de yatirim fonu tarafinda
    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    net_assets: Mapped[Decimal | None] = mapped_column(BigNumType())
    expense_ratio: Mapped[Decimal | None] = mapped_column(PriceType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainTopMover(Base):
    """topPerformingCompanies + topGrowthCompanies -- YALNIZ ENDUSTRI.

    `rank_type` PK'da olmasi VERI KAYBINI onler, sadece cakismayi degil:
    24 endustrilik TAM-LISTE olcumunde 50 ortak sembolun 8'inde iki uc
    FARKLI `ytdReturn` bildiriyor. PK'da olmasaydi biri sessizce kaybolurdu
    (`fund_metrics.section`'in ayni gerekcesi).
    """

    __tablename__ = "domain_top_movers"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rank_type: Mapped[RankType] = mapped_column(RANK_TYPE_ENUM, primary_key=True)
    symbol: Mapped[str] = mapped_column(SymbolType(), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # ELOX (biotechnology) 9999.0 olculdu; SENTINEL SAYILMAZ, oldugu gibi
    # yazilir (`currentPriceTarget = 0.0`'in NULL'a cevrilmemesiyle ayni
    # ilke).
    ytd_return: Mapped[Decimal | None] = mapped_column(PriceType())
    last_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Yalniz `performing` listesinde
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Yalniz `growth` listesinde; RELL 81.5, alt uc -9.999999999999998
    growth_estimate: Mapped[Decimal | None] = mapped_column(PriceType())
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class ResearchReport(Base):
    """Analist raporu -- PAYLASILAN varlik, IKI kaynakli (SQ K8).

    Gunde 624 rapor satiri uretiliyor ama yalniz 516'si TEKIL; 37 tekil
    sektor raporunun HEPSI bir endustride de goruluyor (%100 ortusme). Tek
    tabloda `ERROR 1062` verirdi, bu yuzden rapor + bag tablosu.

    ADI `domain_research_reports` DEGILDIR (SQ S5.4): `Search.research` ayni
    raporlari AYNI kimlik uzayindan dondurur -- bicim
    `<SAGLAYICI>_<KAYNAK_KIMLIK>_<Tur>_<epoch_ms>`, orn.
    `ARGUS_48138_TechnicalAnalysis_1788520901000` (Sector) ve
    `ARGUS_2660_AnalystReport_1785496444000` (Search). Iki ayri tablo ayni
    raporu iki kez, FARKLI kolon altkumeleriyle tutardi.

    Iki kaynak farkli kolonlari doldurur ve BIRBIRINI EZMEZ:
    domain -> `head_html`, `report_title`, `report_type`, hedef fiyat/derece;
    search -> `author`, `report_headline`. Ortak olan `provider` ve
    `report_ts_utc`.
    """

    __tablename__ = "research_reports"
    __table_args__ = (
        Index("ix_research_reports_date", "as_of_date"),
    )

    report_id: Mapped[str] = mapped_column(AsciiKeyType(REPORT_ID_LENGTH), primary_key=True)
    # PK'DA DEGILDIR -- "en son gorulduğu gun". Rapor icerigi degismez;
    # hangi gun hangi domain'de gorundugunu `domain_report_links` tasir.
    # Kolonun VAR OLMASI ise ZORUNLUDUR: `AsOfGate` kapi satirinin
    # `as_of_date`'ini `writes`'in ILK satirindan okur ve bu tablo ilk
    # sirada gelebilir -- kolon olmasaydi KeyError verirdi.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64, collation="C"))
    report_type: Mapped[str | None] = mapped_column(String(64, collation="C"))
    # Olculen max 59
    head_html: Mapped[str | None] = mapped_column(String(255, collation="C"))
    # MEDIUMTEXT OLMAK ZORUNDA: olculen max 23 570 karakter (104 rapor,
    # medyan 281). `TEXT` 65 535 BAYT'tir ve utf8mb4'te 4 baytlik
    # karakterlerle tasabilir.
    report_title: Mapped[str | None] = mapped_column(MEDIUMTEXT())
    # 104 raporun 17'sinde HIC YOK. Ayrica CIPLAK float gelir (oysa
    # topCompanies[].targetPrice SARMALI) -- SI S4.4.
    target_price: Mapped[Decimal | None] = mapped_column(PriceType())
    # Olculen: Maintained / Increased / Decreased / yok
    target_price_status: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # Olculen: Bullish / Neutral / Bearish / yok
    investment_rating: Mapped[str | None] = mapped_column(String(32, collation="C"))
    # DOMAIN yolunda ISO metin, SEARCH yolunda epoch MILISANIYE gelir
    # (SQ S4.3). Ortak bir donusturucu varsayilsaydi biri sessizce NULL
    # olurdu; her dataset kendi donusturucusunu uygular.
    report_ts_utc: Mapped[datetime | None] = mapped_column(TsType())
    # --- yalniz SEARCH yolunda dolar (SQ S5.4) ---
    # Domain yaniti yazar alani TASIMAZ.
    author: Mapped[str | None] = mapped_column(String(128, collation="C"))
    # `Search.research` -> `reportHeadline`. Domain yolunda NULL KALIR:
    # oradaki baslik `head_html` / `report_title` kolonlarinda durur ve
    # ikisi ayni sey degildir (SQ S4.3).
    report_headline: Mapped[str | None] = mapped_column(String(512, collation="C"))
    # `update_columns` DISINDA (AH S5.4)
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainReportLink(Base):
    """Rapor <-> domain bagi, gun bazinda.

    Bolge kolonu YOKTUR: rapor kimlikleri 5 bolgede BIREBIR ayni sirayla
    dondu.
    """

    __tablename__ = "domain_report_links"

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    report_id: Mapped[str] = mapped_column(
        AsciiKeyType(REPORT_ID_LENGTH),
        ForeignKey("research_reports.report_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    # Liste sirasi
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)


class DomainAsOfState(Base):
    """Domain tarafinin as-of KAPISI (SI S5.7).

    NEDEN `asof_state` KULLANILMIYOR: oradaki anahtar (symbol, dataset) ve
    kapi sembolu `row["symbol"]`den okunuyor -- domain tablolarindaki
    `symbol` SIRKETIN sembolu, kapi yanlis varliga yazilirdi. Ayrica bolge
    ekseni oraya sigmaz. Bu, `asof_base.py`'nin `SnapshotDataset` /
    `HashGatedDataset`'i neden kullanmadigini aciklayan gerekcenin aynisi.
    """

    __tablename__ = "domain_asof_state"
    __table_args__ = (
        Index("ix_domain_asof_dataset_date", "dataset", "as_of_date"),
    )

    domain_key: Mapped[str] = domain_key_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(AsciiKeyType(32), primary_key=True)
    # Bolgesiz dataset'ler '*' yazar (market_runner.GLOBAL_SCOPE_MARKER
    # deseni).
    region: Mapped[str] = mapped_column(RegionType(), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(HashType(), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TsType(), nullable=False)
