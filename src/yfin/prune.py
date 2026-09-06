"""Budama (retention) islemleri (S7.4).

TASARIM KARARI: budama VARSAYILAN OLARAK KAPALIDIR. Tarih siniri veren her
komut `YF_PRUNE_ENABLED=true` (ya da `--force`) ister; aksi halde
`PruneDisabledError` firlatir. Gerekce: bu tablolar yeniden cekilemez.
Takvim uclari PENCERE tabanlidir ("tum gecmis" diye bir uc yoktur), yani
silinen bir takvim satiri geri getirilemez; `_history` satirlari ise
tanimi geregi gecmis anlik goruntulerdir ve kaynakta karsiligi yoktur.

Oksuz haber temizligi bunun DISINDADIR: `news` FK ile temizlenemez ve
son baglantisi kalkan haber raw_json tasidigi icin sisme ucuz degildir;
o yuzden varsayilan olarak calisir (onceki spec S5.5).

AS-OF BUDAMASI (AH S5.4) ucuncu bir sinif olusturur ve kendi guvenligini
tasir: her sembolun EN GUNCEL as-of gunu her zaman korunur. Gerekce
mekaniktir -- as-of tablolarinda "yeniden cekilemez" olmanin otesinde bir
tuzak vardir: veri satiri silinse bile `asof_state` kapi satiri yerinde
kalir, bir sonraki calistirmada icerik degismedigi icin hash esitlenir ve
dataset `skipped` deyip HICBIR SEY YAZMAZ. Yani en guncel gun silinseydi
kayip, kaynak hala veriyi verirken bile KALICI olurdu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Table, and_, delete, func, select, tuple_
from sqlalchemy.orm import Session

from yfin.models import Base, News, NewsSymbol
from yfin.models.market import CALENDAR_TIME_COLUMNS

# Sembol tarafinin varsayilanlari; SI S11.2 ile parametre haline geldiler.
# Modul duzeyinde `datasets` paketinden import EDILMEZLER (dongusel
# import), bu yuzden ad olarak tekrarlanirlar -- `test_prune_domain.py`
# ikisinin ayrismadigini surer.
_DEFAULT_GATE = "asof_state"
_DEFAULT_SCOPE_COLUMN = "symbol"


class PruneDisabledError(RuntimeError):
    """Budama kapali (YF_PRUNE_ENABLED=false) ve --force verilmedi."""


@dataclass
class PruneReport:
    """Silinen (ya da --dry-run'da silinecek) satir sayilari."""

    orphan_news: int = 0
    # SI S11.2: `domain_report_links` budandikca bagsiz kalan raporlar
    orphan_reports: int = 0
    calendars: dict[str, int] = field(default_factory=dict)
    history: dict[str, int] = field(default_factory=dict)
    asof: dict[str, int] = field(default_factory=dict)
    domain_asof: dict[str, int] = field(default_factory=dict)
    # SQ S12.2: kesif tablolarinin kapsam kolonu `query_term`dir,
    # `symbol` DEGIL -- varsayilanla budansaydi YANLIS koruma kumesi
    # uretilirdi.
    discovery_asof: dict[str, int] = field(default_factory=dict)
    screens: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def total(self) -> int:
        return (
            self.orphan_news
            + self.orphan_reports
            + sum(self.calendars.values())
            + sum(self.history.values())
            + sum(self.asof.values())
            + sum(self.domain_asof.values())
            + sum(self.discovery_asof.values())
            + sum(self.screens.values())
        )


def history_tables() -> list[str]:
    """`_history` son ekli ve `fetched_at` tasiyan snapshot gecmis tablolari.

    Elle tutulan bir liste yerine metadata'dan turetilir: yeni bir snapshot
    cifti eklendiginde budama kendiliginden onu da kapsar.
    """
    return sorted(
        table.name
        for table in Base.metadata.tables.values()
        if table.name.endswith("_history") and "fetched_at" in table.c
    )


def _symbol_registry() -> Any:
    """Yerel import: modul duzeyinde `datasets` paketine baglanmamak icin."""
    from yfin.datasets import SYMBOL_DATASETS

    return SYMBOL_DATASETS


def asof_tables(
    registry: Any = None,
    gate_table: str = _DEFAULT_GATE,
) -> list[str]:
    """As-of gecmisi BIRIKTIREN tablolar.

    Liste elle tutulmaz ama `Base.metadata` da yeterli DEGILDIR: `as_of_date`
    kolonunu PK'sinda tasiyan her tablo as-of degildir -- `shares_full`
    kaynagin KENDI tarihini tasir ve silinse watermark ile yeniden cekilir.
    Ayirt edici isaret dataset'in TABANIDIR, kolon adi degil.

    Kapi tablosunun KENDISI KAPSAM DISIDIR: kapi satiri silinseydi
    `first_seen_at` kaybolur ve butun gecmis bir sonraki kosuda yeniden
    yazilirdi.
    """
    return sorted(asof_table_datasets(registry, gate_table))


def asof_table_datasets(
    registry: Any = None,
    gate_table: str = _DEFAULT_GATE,
) -> dict[str, list[str]]:
    """As-of tablosu -> ona yazan DATASET adlari.

    Cogu tabloyu tek dataset yazar, ama `institutional_holders`'i IKI
    dataset yazar (`institutional_holders` + `mutualfund_holders`,
    `holder_type` ile ayrisirlar); domain tarafinda `domain_top_companies`'i
    `sector_rankings` ve `industry_rankings` birlikte yazar. Koruma bu
    ayrimi bilmek ZORUNDADIR; gerekce `_asof_protected`'ta.

    Parametreler SI S11.2 ile eklendi. Varsayilanlari sembol tarafidir, bu
    yuzden mevcut cagrilarin davranisi BIREBIR aynidir. Taban sinif kontrolu
    `AsOfGate` uzerinden yapilir: `AsOfDataset` ve `DomainAsOfDataset`
    ortak mixin'i paylasir, ortak bir `Dataset` atasi YOKTUR.
    """
    from yfin.datasets.asof_base import AsOfGate

    reg = _symbol_registry() if registry is None else registry
    mapping: dict[str, list[str]] = {}
    for name in reg:
        dataset = reg[name]
        if not isinstance(dataset, AsOfGate):
            continue
        # KAPI TABLOSUNA GORE SUZULUR, yalnizca tipe gore DEGIL.
        #
        # SQ ile birlikte ayni registry'de IKI kapi ailesi var: `search` ve
        # `lookup` da `AsOfGate`tir ama `discovery_asof_state` kullanir
        # (SQ K3a). Suzgec olmasaydi `prune_asof(gate_table="asof_state")`
        # onlarin tablolarini YANLIS KAPIYLA budamaya calisirdi: koruma
        # kumesi `asof_state`ten okunur, oysa o tabloda kesif satiri hic
        # yoktur -- yani SON GUN DE KORUNMAZDI.
        if dataset.asof_gate_table != gate_table:
            continue
        for table in dataset.produces:
            if table == gate_table:
                continue
            # `as_of_date` TASIMAYAN hedefler (`symbols`, `news`,
            # `news_symbols`, `research_reports`) as-of budamasinin konusu
            # degildir; `prune_asof` onlari zaten atlardi ama haritada
            # gorunmeleri "budaniyor" izlenimi verirdi.
            if "as_of_date" not in Base.metadata.tables[table].c:
                continue
            mapping.setdefault(table, []).append(name)
    return mapping


def _asof_protected(
    session: Session,
    table: Table,
    before: date,
    datasets: list[str],
    *,
    gate_table: str = _DEFAULT_GATE,
    scope_column: str = _DEFAULT_SCOPE_COLUMN,
) -> list[tuple[str, date]]:
    """Korunacak (sembol, as_of_date) ciftleri -- IKI kaynagin BIRLESIMI.

    1. `asof_state` KAPI satirlari (dataset BASINA en guncel gun). Tabloyu
       birden fazla dataset yaziyorsa otorite budur: tablo uzerinden
       `GROUP BY symbol` yapmak, `mutualfund_holders`'in en guncel gunu
       `institutional_holders`'inkinden ESKIYSE onu korumasiz birakir ve
       satirlari SILINIR. Kayip KALICIDIR: kapi satiri silinmedigi icin
       bir sonraki kosu hash'i esit bulup `skipped` der, hicbir sey
       yazmaz -- bu modulun docstring'inde anlatilan tuzagin ta kendisi.
    2. Tablo uzerinden `GROUP BY symbol` (eski davranis). Kapi satiri
       herhangi bir nedenle yoksa (elle yazilmis veri, kapi tablosundan
       once yazilmis satirlar) koruma bosa dusmesin diye BIRLESIME dahil
       edilir; fazladan koruma budamayi yalnizca daha tutucu yapar.
    """
    gate = Base.metadata.tables[gate_table]
    # `domain_asof_state` PK'sinda `region` DE var; koruma ciftleri
    # (domain_key, as_of_date)'e indirgenir -- bolgeler arasinda DAHA
    # TUTUCU koruma demektir, veri kaybi yonunde degil (SI S11.2).
    gate_stmt = select(gate.c[scope_column], gate.c["as_of_date"]).where(
        gate.c["dataset"].in_(datasets)
    )
    table_stmt = (
        select(table.c[scope_column], func.max(table.c["as_of_date"]))
        .group_by(table.c[scope_column])
        .having(func.max(table.c["as_of_date"]) < before)
    )
    pairs = {
        (str(scope), as_of)
        for stmt in (gate_stmt, table_stmt)
        for scope, as_of in session.execute(stmt)
        if as_of is not None
    }
    return sorted(pairs)


def prune_asof(
    session: Session,
    before: date,
    *,
    dry_run: bool = False,
    registry: Any = None,
    gate_table: str = _DEFAULT_GATE,
    scope_column: str = _DEFAULT_SCOPE_COLUMN,
) -> dict[str, int]:
    """Verilen gunden eski as-of satirlarini siler; SON GUNU korur.

    (registry, gate_table, scope_column) ucluslu parametrelestirme SI
    S11.2'dendir. Domain tarafi icin
    `(DOMAIN_DATASETS, "domain_asof_state", "domain_key")` verilir --
    `symbol` ile gruplamak YANLIS koruma kapsami uretirdi: domain
    tablolarindaki `symbol` SIRKETIN sembolu.
    """
    removed: dict[str, int] = {}
    for name, datasets in sorted(asof_table_datasets(registry, gate_table).items()):
        table = Base.metadata.tables[name]
        # KAPSAM ELEMESI, elle tutulan bir liste DEGIL: budama
        # (scope_column, as_of_date) ciftiyle calisir, bu yuzden iki
        # kolondan biri olmayan hedef zaten budanamaz.
        #   * `research_reports`: PK (report_id), `domain_key`
        #     kolonu YOK -- tablo PAYLASIMLIDIR. Oksuz raporlar
        #     `prune_orphan_reports` ile temizlenir.
        #   * `domains`: `as_of_date` kolonu yok; statik kimlik tablosu.
        if scope_column not in table.c or "as_of_date" not in table.c:
            continue
        where = table.c["as_of_date"] < before
        protected = _asof_protected(
            session,
            table,
            before,
            datasets,
            gate_table=gate_table,
            scope_column=scope_column,
        )
        if protected:
            where = and_(
                where,
                tuple_(table.c[scope_column], table.c["as_of_date"]).not_in(protected),
            )
        if dry_run:
            stmt = select(func.count()).select_from(table).where(where)
            removed[name] = int(session.execute(stmt).scalar_one())
            continue
        deleted = session.execute(table.delete().where(where))
        removed[name] = int(deleted.rowcount)  # type: ignore[attr-defined]
    return removed


def prune_orphan_reports(session: Session, *, dry_run: bool = False) -> int:
    """`domain_report_links`ta karsiligi kalmayan raporlari siler (SI S11.2).

    `research_reports` BUDANMAZ ama `domain_report_links` budandikca
    hicbir bagi kalmayan raporlar birikir. FK `ON DELETE CASCADE` TERS
    YONDE calisir (rapor silinince bag silinir), bu yuzden `news`teki
    `prune_orphan_news` muadili ayri bir adim gerekir.
    """
    from yfin.models import DomainReportLink, ResearchReport, SearchReportHit

    # IKI bag tablosu kontrol edilir (SQ S12.2/4). `search_report_hits`
    # eklenmeseydi Search yolunun buldugu HER rapor -- domain tarafinda
    # bagi olmadigi icin -- yetim sayilip SILINIRDI. Tablo adi
    # `domain_report_links` olarak kaldi ama artik tek bag degil.
    domain_linked = select(DomainReportLink.report_id)
    search_linked = select(SearchReportHit.report_id)
    unlinked = ResearchReport.report_id.not_in(domain_linked) & ResearchReport.report_id.not_in(
        search_linked
    )
    if dry_run:
        stmt = select(func.count()).select_from(ResearchReport).where(unlinked)
        return int(session.execute(stmt).scalar_one())
    result = session.execute(delete(ResearchReport).where(unlinked))
    return int(result.rowcount)  # type: ignore[attr-defined]


def prune_orphan_news(session: Session, *, dry_run: bool = False) -> int:
    """news_symbols'ta karsiligi kalmayan haberleri siler (S5.5)."""
    linked = select(NewsSymbol.news_id)
    if dry_run:
        stmt = select(func.count()).select_from(News).where(News.news_id.not_in(linked))
        return int(session.execute(stmt).scalar_one())
    result = session.execute(delete(News).where(News.news_id.not_in(linked)))
    return int(result.rowcount)  # type: ignore[attr-defined]


def _prune_by_time(
    session: Session, tables: dict[str, str], before: datetime, *, dry_run: bool
) -> dict[str, int]:
    removed: dict[str, int] = {}
    for name, column in tables.items():
        table = Base.metadata.tables[name]
        where = table.c[column] < before
        if dry_run:
            stmt = select(func.count()).select_from(table).where(where)
            removed[name] = int(session.execute(stmt).scalar_one())
            continue
        deleted = session.execute(table.delete().where(where))
        removed[name] = int(deleted.rowcount)  # type: ignore[attr-defined]
    return removed


def prune_calendars(session: Session, before: datetime, *, dry_run: bool = False) -> dict[str, int]:
    """Verilen tarihten eski takvim satirlarini siler.

    Takvim tablolari birikir ve sembolden bagimsiz olduklari icin FK ile
    temizlenemez; tek temizlik yolu budur.
    """
    return _prune_by_time(session, CALENDAR_TIME_COLUMNS, before, dry_run=dry_run)


def prune_history(session: Session, before: datetime, *, dry_run: bool = False) -> dict[str, int]:
    """Verilen tarihten eski `_history` anlik goruntulerini siler.

    En hizli buyuyen tablo `market_summary_history`'dir: her kosuda fiyat
    degistigi icin content_hash kapisi onu elemez.
    """
    tables = dict.fromkeys(history_tables(), "fetched_at")
    return _prune_by_time(session, tables, before, dry_run=dry_run)


def prune_screens(session: Session, before: date, *, dry_run: bool = False) -> dict[str, int]:
    """Ekran tablolarini budar (SQ S12.2/2-3).

    `prune_asof` BURADA KULLANILAMAZ, iki bagimsiz nedenle:

    1. `screener` bir `HashGate`dir, `AsOfGate` DEGIL -- `asof_table_datasets`
       onu hic gormez ve fonksiyon bos sozluk dondururdu.
    2. `screen_runs`ta `dataset` kolonu YOKTUR; `gate.c["dataset"]` KeyError
       verirdi.

    Ayrica `screen_quotes` KAPISIZDIR: `screen_key` kolonu yoktur (SQ K5,
    kotasyon ekrandan bagimsizdir), yani kapsam sutunuyla gruplanamaz.
    Onun icin sembol basina SON GUN korunur.
    """
    from yfin.models.discovery import ScreenMember, ScreenRun, screen_quotes

    removed: dict[str, int] = {}

    # 1. Uyelik: her ekranin SON GUNU korunur (as-of budamasinin ilkesi).
    latest = (
        select(ScreenRun.screen_key, func.max(ScreenRun.as_of_date).label("keep"))
        .group_by(ScreenRun.screen_key)
        .subquery()
    )
    member_cond = ScreenMember.as_of_date < before
    protected = select(latest.c.screen_key, latest.c.keep)
    keep_pairs = {(k, d) for k, d in session.execute(protected).all()}

    def _member_rows() -> list[tuple[str, date]]:
        stmt = select(ScreenMember.screen_key, ScreenMember.as_of_date).where(member_cond)
        return [(k, d) for k, d in session.execute(stmt).all() if (k, d) not in keep_pairs]

    victims = _member_rows()
    if victims:
        if dry_run:
            removed["screen_members"] = len(victims)
        else:
            count = 0
            for key, day in {(k, d) for k, d in victims}:
                result = session.execute(
                    delete(ScreenMember).where(
                        ScreenMember.screen_key == key, ScreenMember.as_of_date == day
                    )
                )
                count += int(result.rowcount)  # type: ignore[attr-defined]
            removed["screen_members"] = count
            session.execute(
                delete(ScreenRun).where(
                    ScreenRun.as_of_date < before,
                    tuple_(ScreenRun.screen_key, ScreenRun.as_of_date).not_in(keep_pairs),
                )
            )

    # 2. Kotasyon: KAPISIZ, sembol basina son gun korunur.
    keep_quotes = (
        select(screen_quotes.c.symbol, func.max(screen_quotes.c.as_of_date).label("keep"))
        .group_by(screen_quotes.c.symbol)
        .subquery()
    )
    quote_cond = screen_quotes.c.as_of_date < before
    quote_cond &= tuple_(screen_quotes.c.symbol, screen_quotes.c.as_of_date).not_in(
        select(keep_quotes.c.symbol, keep_quotes.c.keep)
    )
    if dry_run:
        n = session.execute(
            select(func.count()).select_from(screen_quotes).where(quote_cond)
        ).scalar_one()
        if n:
            removed["screen_quotes"] = int(n)
    else:
        result = session.execute(delete(screen_quotes).where(quote_cond))
        if result.rowcount:  # type: ignore[attr-defined]
            removed["screen_quotes"] = int(result.rowcount)  # type: ignore[attr-defined]
    return removed


def run_prune(
    session: Session,
    *,
    enabled: bool,
    orphan_news: bool = True,
    orphan_reports: bool = True,
    calendars_before: datetime | None = None,
    history_before: datetime | None = None,
    asof_before: datetime | None = None,
    dry_run: bool = False,
) -> PruneReport:
    """Budama akisinin tek giris noktasi.

    `enabled` False iken tarih sinirli budama CALISMAZ: bu, "ozellik var ama
    varsayilan kapali" kuralinin tek zorlandigi yerdir.
    """
    if (calendars_before or history_before or asof_before) and not enabled:
        raise PruneDisabledError(
            "budama kapali: YF_PRUNE_ENABLED=true yapin ya da --force verin"
        )

    report = PruneReport(dry_run=dry_run)
    if orphan_news:
        report.orphan_news = prune_orphan_news(session, dry_run=dry_run)
    if calendars_before:
        report.calendars = prune_calendars(session, calendars_before, dry_run=dry_run)
    if history_before:
        report.history = prune_history(session, history_before, dry_run=dry_run)
    if asof_before:
        report.asof = prune_asof(session, asof_before.date(), dry_run=dry_run)
        # IKINCI CAGRI: domain uclusuyle. `symbol` ile gruplamak YANLIS
        # koruma kapsami uretirdi -- domain tablolarindaki `symbol`
        # SIRKETIN sembolu (SI S11.2).
        from yfin.datasets.registry import DOMAIN_DATASETS, SYMBOL_DATASETS

        report.domain_asof = prune_asof(
            session,
            asof_before.date(),
            dry_run=dry_run,
            registry=DOMAIN_DATASETS,
            gate_table="domain_asof_state",
            scope_column="domain_key",
        )
        # UCUNCU CAGRI: kesif uclusuyle (SQ S12.2/1). Bes tablonun besi de
        # `query_term` + `as_of_date` tasir ve kapi `dataset` kolonu tasir,
        # yani mevcut fonksiyon OLDUGU GIBI calisir. Varsayilan
        # `scope_column="symbol"` ile cagrilsaydi `search_lists` ve
        # `lookup_totals` (ki `symbol` kolonlari YOK) sessizce ATLANIR,
        # `search_quotes`/`lookup_results` ise YANLIS kapsamla budanirdi:
        # koruma kumesi terime gore degil sembole gore secilirdi.
        report.discovery_asof = prune_asof(
            session,
            asof_before.date(),
            dry_run=dry_run,
            registry=SYMBOL_DATASETS,
            gate_table="discovery_asof_state",
            scope_column="query_term",
        )
        report.screens = prune_screens(session, asof_before.date(), dry_run=dry_run)
    # Oksuz rapor temizligi as-of budamasindan SONRA gelmelidir: bagi
    # kaldiran adim odur.
    if orphan_reports:
        report.orphan_reports = prune_orphan_reports(session, dry_run=dry_run)
    if not dry_run:
        session.commit()
    return report
