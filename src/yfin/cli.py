"""Komut satiri arayuzu (S11)."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Annotated, Any

import typer
from sqlalchemy import Engine, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from yfin import normalize as nz
from yfin import proxy as px
from yfin.cli_bars import bars_app, scope_app
from yfin.cli_config import config_app
from yfin.config import SETTINGS_SOURCE_VAR, bootstrap_settings, get_settings
from yfin.datasets import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS
from yfin.db import LockNotAcquired, create_db_engine
from yfin.domain_audit import audit_domains
from yfin.domain_runner import RegionValidationError, run_domain_sync
from yfin.logging_setup import configure_logging, get_logger
from yfin.market_runner import run_market_sync
from yfin.models import (
    Base,
    Domain,
    DomainType,
    NewsSymbol,
    Proxy,
    ProxyHealth,
    ProxyScheme,
    RunScope,
    Symbol,
    SyncRun,
    SyncRunItem,
    symbol_scoped_tables,
)
from yfin.models.discovery import QUERY_TERM_LENGTH
from yfin.prune import PruneDisabledError, run_prune
from yfin.runner import EXIT_LOCK_NOT_ACQUIRED, EXIT_NO_PROXY, run_sync
from yfin.shard import NoEligibleProxy, run_sharded

log = get_logger(__name__)

app = typer.Typer(
    help="yfinance -> PostgreSQL 18 + TimescaleDB veri hatti", no_args_is_help=True
)
db_app = typer.Typer(help="Veritabani islemleri", no_args_is_help=True)
symbols_app = typer.Typer(help="Sembol evreni yonetimi", no_args_is_help=True)
proxy_app = typer.Typer(help="Proxy havuzu yonetimi", no_args_is_help=True)
app.add_typer(db_app, name="db")
market_app = typer.Typer(help="Piyasa (Market/Calendars) verisi", no_args_is_help=True)
screen_app = typer.Typer(help="Screener ekranlari (SQ S13.2)", no_args_is_help=True)
discover_app = typer.Typer(help="Serbest terim kesfi (SQ S8.2)", no_args_is_help=True)
domain_app = typer.Typer(help="Sektor / endustri verisi", no_args_is_help=True)
app.add_typer(symbols_app, name="symbols")
app.add_typer(proxy_app, name="proxy")
app.add_typer(market_app, name="market")
app.add_typer(screen_app, name="screen")
app.add_typer(discover_app, name="discover")
app.add_typer(domain_app, name="domain")
# price_bars komutlari ayri modulde (PB S11): cli.py 630 satirdi ve bu
# komutlarin hicbiri mevcut komutlarla durum paylasmiyor.
app.add_typer(bars_app, name="bars")
app.add_typer(scope_app, name="scope")
# DB tabanli yapilandirma (CFG S6.2). Ayri modulde: hicbir komutu
# mevcutlarla durum paylasmiyor ve cli.py zaten 1000 satiri asti.
app.add_typer(config_app, name="config")


def _parse_date(value: str | None) -> datetime | None:
    """YYYY-MM-DD -> UTC-AWARE datetime.

    Kolonlar `timestamptz`tir (PG S2.3); naive bir sinir degeri
    psycopg tarafindan baglanti TZ'sine gore yorumlanirdi -- sonuc dogru
    cikar ama karsilastirma farkli farkindalik duzeyinde kalirdi.
    """
    return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC) if value else None


def _parse_day(value: str | None, *, option: str) -> date | None:
    """YYYY-MM-DD -> date. Gecersiz deger sessizce None olmaz: aralik
    yanlissa kullanici "aralik uygulandi" sanirdi."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        typer.echo(f"{option} icin gecersiz tarih: {value} (YYYY-MM-DD bekleniyor)", err=True)
        raise typer.Exit(code=1) from None


def _csv_upper(value: str | None) -> list[str]:
    return [part.strip().upper() for part in (value or "").split(",") if part.strip()]


def _selector(
    *,
    exchange: list[str],
    quote_type: list[str],
    suffix: str | None,
    start: date | None,
    end: date | None,
) -> str | None:
    """Calistirmanin sembol evrenini ve araligini INSAN-OKUNUR kaydeder.

    `scope` kolonu yalnizca symbols/market ayrimini tasiyor; hangi run'in
    hangi evreni kapsadigi aksi halde geriye donuk bilinemez ve
    "eksiksizlik" iddiasi denetlenemez (AH S5.6).
    """
    parts: list[str] = []
    if exchange:
        parts.append(f"exchange={','.join(exchange)}")
    if quote_type:
        parts.append(f"quote_type={','.join(quote_type)}")
    if suffix:
        parts.append(f"suffix={suffix}")
    if start is not None:
        parts.append(f"start={start.isoformat()}")
    if end is not None:
        parts.append(f"end={end.isoformat()}")
    return " ".join(parts)[:255] or None


def _normalize_filter_values(values: list[str]) -> list[str]:
    """--exchange / --quote-type girdisini yazma yolunun bicimine cevirir.

    `datasets/symbols.py` bu iki kolonu `.upper()` ile yaziyor; filtre de
    ayni bicime gelmezse `--exchange nms` SESSIZCE bos sonuc dondururdu
    (PG S2.5.1).
    """
    return [v.strip().upper() for v in values]


def _filtered_symbols(
    session: Session,
    base_stmt: Any,
    *,
    exchanges: list[str],
    quote_types: list[str],
    suffix: str | None,
) -> list[str]:
    """--exchange / --quote-type / --suffix filtreleri; AND'lenir (AH S6.4).

    Girdi `.upper()` ile normalize edilir. Kolonlar artik COLLATE "C"dir
    (duyarli) ve YAZMA YOLU da buyuk harfe cevirir (datasets/symbols.py),
    yani iki taraf ayni bicimde bulusur (PG S2.5.1).

    `func.upper` YINE KULLANILMAZ ama gerekcesi degisti: eskiden kolon
    zaten duyarsiz oldugu icin gereksizdi; simdi kolonu fonksiyonla
    sarmalamak `ix_symbols_exchange` indeksini kullanilamaz hale
    getirecegi icin kacinilir. Normalizasyon KOLONDA degil GIRDIDE yapilir.
    """
    stmt = base_stmt
    if exchanges:
        stmt = stmt.where(Symbol.exchange.in_(_normalize_filter_values(exchanges)))
    if quote_types:
        stmt = stmt.where(Symbol.quote_type.in_(_normalize_filter_values(quote_types)))
    if suffix:
        # Borsa COZULMEDEN de calisir; NULL tuzagina takilmaz.
        stmt = stmt.where(Symbol.symbol.like(f"%{suffix.strip().upper()}"))
    codes = list(session.execute(stmt).scalars())

    # NULL TUZAGI: exchange/quote_type kolonlarini bootstrap `symbols`
    # dataset'i doldurur; `yfin symbols add` ile eklenen sembolde ILK
    # SYNC'E KADAR NULL'durlar ve filtre onlari sessizce elerdi.
    null_columns = [
        (name, column)
        for name, column, active in (
            ("exchange", Symbol.exchange, bool(exchanges)),
            ("quote_type", Symbol.quote_type, bool(quote_types)),
        )
        if active
    ]
    if null_columns:
        unresolved = list(
            session.execute(
                base_stmt.where(or_(*(column.is_(None) for _, column in null_columns)))
            ).scalars()
        )
        if unresolved:
            names = "/".join(name for name, _ in null_columns)
            typer.echo(
                f"{len(unresolved)} sembol filtre disinda birakildi "
                f"({names} NULL - henuz cozulmemis)."
            )
            typer.echo(
                "Once 'yfin sync --datasets symbols' calistirin ya da --suffix kullanin."
            )
    return codes


def _engine() -> Engine:
    configure_logging(get_settings().log_level)
    return create_db_engine()


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=_engine(), expire_on_commit=False, future=True)


# --------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Hedef revizyon")] = "head",
) -> None:
    """Alembic migration'larini uygular."""
    from alembic import command
    from alembic.config import Config

    configure_logging(get_settings().log_level)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, revision)
    typer.echo(f"migration uygulandi: {revision}")
    _warn_missing_settings_rows()


def _warn_missing_settings_rows() -> None:
    """Satiri OLMAYAN DB-yonetimli anahtarlari tek satirda uyarir (CFG S5.4).

    Goc sonrasi `.env` katmani fiilen BOSTUR; `Settings`e sonradan eklenen
    bir alan icin `yfin config seed` calistirilmazsa deger artik `.env`e
    degil DOGRUDAN model varsayilanina duser. Bu yuzden `seed` her
    `upgrade` sonrasi standart adimdir ve komut bunu HATIRLATIR.

    Komut `Settings.model_fields`i okur; bu bir MIGRATION degil bir
    KOMUTTUR, dolayisiyla "migration uygulama kodunu import etmesin"
    ilkesi ihlal edilmez (CFG S5.4).
    """
    from yfin.config import DB_MANAGED_FIELDS, bootstrap_settings, source_is_env
    from yfin.settings_store import fetch_rows

    if source_is_env():
        return
    try:
        rows = fetch_rows(bootstrap_settings())
    except Exception as exc:  # noqa: BLE001 - uyari yolu, komutu coktrmez
        typer.echo(f"settings tablosu okunamadi, eksik satir denetimi atlandi: {exc}", err=True)
        return
    if rows is None:
        return
    missing = sorted(DB_MANAGED_FIELDS - set(rows))
    if missing:
        typer.echo(
            f"{len(missing)} ayarin `settings` satiri yok (ilki: {missing[0]}); "
            "degerleri .env ya da model varsayilanindan gelecek. "
            "`yfin config seed` calistirin."
        )


@db_app.command("create")
def db_create() -> None:
    """Veritabani semalarini olusturur (yoksa).

    DB YAPILANDIRMA KATMANINI HIC KULLANMAZ (CFG S7): veritabanini
    YARATAN komut, var olmayan veritabanina baglanamaz. `get_settings()`
    cagrilsaydi yukleyici `settings` tablosunu okumak icin tam o
    veritabanina baglanmayi denerdi.
    """
    from sqlalchemy import create_engine

    settings = bootstrap_settings()
    # AUTOCOMMIT SART: `CREATE DATABASE` PostgreSQL'de transaction blogu
    # icinde CALISMAZ.
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        for name in (settings.db_name, settings.db_test_name):
            # `CREATE DATABASE IF NOT EXISTS` PostgreSQL'de YOKTUR;
            # varlik kontrolu pg_database uzerinden yapilir.
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()

    # Eklenti HER VERITABANINDA AYRI kurulur: `CREATE EXTENSION`
    # veritabani duzeyindedir. Migration'a KONMAZ -- `downgrade` ile
    # simetrisi bozulurdu ve eklenti `db create`in urunudur (PG S12).
    for name in (settings.db_name, settings.db_test_name):
        db_engine = create_engine(settings.db_url(name), isolation_level="AUTOCOMMIT")
        with db_engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        db_engine.dispose()

    typer.echo(f"veritabani hazir: {settings.db_name}, {settings.db_test_name}")


@db_app.command("revision")
def db_revision(message: Annotated[str, typer.Option("-m", "--message")]) -> None:
    """Modellerden yeni bir migration uretir.

    DB yapilandirma katmani KAPATILIR (CFG S7). `migrations/env.py`
    `get_settings()` cagirir; sema henuz olusmadan calistirilan bir
    `revision` aksi halde `settings` tablosunu ararken patlardi. Ortam
    degiskeni kurulur, cunku katmani kapatan anahtarin kendisi
    katmandan okunamaz (tavuk-yumurta).
    """
    import os

    from alembic import command
    from alembic.config import Config

    os.environ[SETTINGS_SOURCE_VAR] = "env"
    command.revision(Config("alembic.ini"), message=message, autogenerate=True)


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------


@symbols_app.command("add")
def symbols_add(
    symbols: Annotated[list[str], typer.Argument(help="Sembol kodlari")],
) -> None:
    """Sembol ekler. strip().upper() uygulanir (S8.3)."""
    factory = _session_factory()
    added, existing = [], []
    with factory() as session:
        for raw in symbols:
            code = nz.normalize_symbol(raw)
            if not code:
                continue
            if session.get(Symbol, code) is not None:
                existing.append(code)
                continue
            session.add(Symbol(symbol=code, is_active=True))
            added.append(code)
        session.commit()
    if added:
        typer.echo(f"eklendi: {', '.join(added)}")
    if existing:
        typer.echo(f"zaten var: {', '.join(existing)}")


@symbols_app.command("list")
def symbols_list(
    include_inactive: Annotated[bool, typer.Option("--include-inactive")] = False,
) -> None:
    """Sembol evrenini listeler."""
    factory = _session_factory()
    stmt = select(Symbol).order_by(Symbol.symbol)
    if not include_inactive:
        stmt = stmt.where(Symbol.is_active.is_(True))
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
    if not rows:
        typer.echo("sembol yok")
        return
    for row in rows:
        flag = "" if row.is_active else " [inactive]"
        typer.echo(f"{row.symbol:<12} {row.quote_type or '-':<10} {row.short_name or ''}{flag}")


@symbols_app.command("exchanges")
def symbols_exchanges(
    include_inactive: Annotated[bool, typer.Option("--include-inactive")] = False,
) -> None:
    """Evrendeki (exchange, tam ad, tip) uclulerini sayilariyla listeler.

    --exchange/--quote-type degerlerinin KESFEDILEBILIR olmasi icin vardir;
    NULL satiri "henuz cozulmemis" sembolleri gorunur kilar (AH S6.4).
    """
    factory = _session_factory()
    stmt = (
        select(
            Symbol.exchange,
            Symbol.full_exchange_name,
            Symbol.quote_type,
            func.count().label("adet"),
        )
        .group_by(Symbol.exchange, Symbol.full_exchange_name, Symbol.quote_type)
        .order_by(func.count().desc())
    )
    if not include_inactive:
        stmt = stmt.where(Symbol.is_active.is_(True))
    with factory() as session:
        rows = session.execute(stmt).all()
    if not rows:
        typer.echo("sembol yok")
        return
    for exchange, full_name, quote_type, count in rows:
        code = exchange or "NULL (cozulmemis)"
        typer.echo(f"{code:<20} {quote_type or '-':<16} {full_name or '':<32} {count}")


@symbols_app.command("deactivate")
def symbols_deactivate(symbol: str) -> None:
    """Soft delete: is_active=0. Veri silinmez (S5.5)."""
    code = nz.normalize_symbol(symbol)
    factory = _session_factory()
    with factory() as session:
        result = session.execute(
            update(Symbol).where(Symbol.symbol == code).values(is_active=False)
        )
        changed = result.rowcount  # type: ignore[attr-defined]
        session.commit()
    typer.echo(f"pasiflestirildi: {code}" if changed else f"bulunamadi: {code}")


@symbols_app.command("purge")
def symbols_purge(
    symbol: str,
    force: Annotated[bool, typer.Option("--force", help="Gercek silme; VERI KAYBI")] = False,
) -> None:
    """Gercek silme (S5.5). ON DELETE RESTRICT nedeniyle ilgili satirlar
    acikca, sirayla silinir."""
    code = nz.normalize_symbol(symbol)
    if not force:
        typer.echo("bu komut geri donusumsuzdur; onaylamak icin --force verin")
        raise typer.Exit(code=1)

    factory = _session_factory()
    with factory() as session:
        for name in symbol_scoped_tables():
            table = Base.metadata.tables[name]
            session.execute(delete(table).where(table.c["symbol"] == code))
        session.execute(delete(NewsSymbol).where(NewsSymbol.symbol == code))
        session.execute(delete(Symbol).where(Symbol.symbol == code))
        session.commit()
    typer.echo(f"silindi: {code}")


# --------------------------------------------------------------------------
# sync / status / prune
# --------------------------------------------------------------------------


@app.command("sync")
def sync(
    symbols: Annotated[str | None, typer.Option("--symbols", help="Virgullu liste")] = None,
    datasets: Annotated[str, typer.Option("--datasets")] = "all",
    full_refresh: Annotated[bool, typer.Option("--full-refresh")] = False,
    include_inactive: Annotated[bool, typer.Option("--include-inactive")] = False,
    shards: Annotated[
        int | None, typer.Option("--shards", help="YF_MAX_SHARDS'i gecici olarak ezer")
    ] = None,
    no_proxy: Annotated[
        bool, typer.Option("--no-proxy", help="Havuzu yok say; tek shard, dogrudan baglanti")
    ] = False,
    require_proxy: Annotated[
        bool, typer.Option("--require-proxy", help="Uygun proxy yoksa calisma")
    ] = False,
    start: Annotated[str | None, typer.Option("--start", help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end", help="YYYY-MM-DD")] = None,
    exchange: Annotated[
        str | None, typer.Option("--exchange", help="Virgullu borsa kodu (NMS,NYQ,IST)")
    ] = None,
    quote_type: Annotated[
        str | None, typer.Option("--quote-type", help="Virgullu tip (EQUITY,ETF)")
    ] = None,
    suffix: Annotated[
        str | None, typer.Option("--suffix", help="Sembol soneki (.IS) -- borsa cozulmeden calisir")
    ] = None,
) -> None:
    """Veri cekip PostgreSQL'e yazar.

    Proxy havuzunda uygun proxy varsa calistirma SHARD'LARA bolunur:
    proxy basina bir OS process. Onarim (history repair) gecmisi geriye
    donuk degistirebildigi icin duzeltmelerin tamaminin inmesi periyodik
    --full-refresh gerektirir.

    --start/--end araligi `date_range="api"` dataset'lerinde GERCEK geriye
    donuk cekim, `"filter"` olanlarda satir elemesi yapar; `"none"` olanlar
    HIC CALISTIRILMAZ (AH S6.2).
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = SYMBOL_DATASETS.resolve(None if datasets.strip() == "all" else datasets.split(","))

    start_date = _parse_day(start, option="--start")
    end_date = _parse_day(end, option="--end")
    if start_date is not None and end_date is not None and start_date > end_date:
        typer.echo("--start --end'den sonra olamaz", err=True)
        raise typer.Exit(code=1)

    exchanges = _csv_upper(exchange)
    quote_types = _csv_upper(quote_type)
    filtered = bool(exchanges or quote_types or suffix)

    if symbols and filtered:
        # Ikisi ayni anda verilseydi hangisinin kazandigi sessiz bir
        # varsayim olurdu; kullanici evreni ya acikca sayar ya filtreler.
        typer.echo("--symbols ile --exchange/--quote-type/--suffix birlikte kullanilamaz", err=True)
        raise typer.Exit(code=1)

    if (start_date is not None or end_date is not None) and all(
        d.date_range == "none" for d in selected if d.name != SYMBOL_DATASETS.bootstrap
    ):
        typer.echo(
            "secilen dataset'lerin hicbiri tarih araligi desteklemiyor; "
            "--start/--end kaldirin ya da 'history', 'upgrades_downgrades' gibi "
            "bir dataset secin",
            err=True,
        )
        raise typer.Exit(code=1)

    if symbols:
        codes = [nz.normalize_symbol(s) for s in symbols.split(",") if s.strip()]
    else:
        stmt = select(Symbol.symbol).order_by(Symbol.symbol)
        if not include_inactive:
            stmt = stmt.where(Symbol.is_active.is_(True))
        with sessionmaker(bind=engine)() as session:
            if filtered:
                codes = _filtered_symbols(
                    session, stmt, exchanges=exchanges, quote_types=quote_types, suffix=suffix
                )
            else:
                codes = list(session.execute(stmt).scalars())

    if not codes:
        typer.echo("sembol yok; once 'yfin symbols add ...' calistirin")
        raise typer.Exit(code=0)

    try:
        # Kilit koordinatorde alinir (S8.7); child'lar kilit ALMAZ.
        # Gecikmis bir cron tetiklemesi calisan sync'in uzerine binemez.
        tally = run_sharded(
            engine,
            codes,
            [d.name for d in selected],
            settings=settings,
            full_refresh=full_refresh,
            max_shards=shards,
            no_proxy=no_proxy,
            require_proxy=require_proxy,
            start=start_date,
            end=end_date,
            selector=_selector(
                exchange=exchanges,
                quote_type=quote_types,
                suffix=suffix,
                start=start_date,
                end=end_date,
            ),
        )
    except LockNotAcquired:
        typer.echo("baska bir sync calisiyor (advisory lock alinamadi)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None
    except NoEligibleProxy as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_NO_PROXY) from None

    _echo_tally(tally)
    raise typer.Exit(code=tally.exit_code())


@app.command("status")
def status(
    limit: Annotated[int, typer.Option("--limit")] = 1,
    scope: Annotated[str | None, typer.Option("--scope", help="symbols | market")] = None,
) -> None:
    """Son sync run ozeti."""
    factory = _session_factory()
    with factory() as session:
        stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
        if scope:
            stmt = select(SyncRun).where(SyncRun.scope == RunScope(scope.strip().lower()))
            stmt = stmt.order_by(SyncRun.started_at.desc()).limit(limit)
        runs = list(session.execute(stmt).scalars())
        if not runs:
            typer.echo("henuz sync calistirilmadi")
            return
        for run in runs:
            # Piyasa run'inda "sembol" diye bir sey yoktur; bolge sayisi
            # symbol_count'a YAZILMAZ (S7.2), bu yuzden etiket de degisir
            keyless = run.scope in (RunScope.MARKET, RunScope.DOMAIN)
            unit = "bolgeler" if run.scope is RunScope.MARKET else "semboller"
            if run.scope is RunScope.DOMAIN:
                unit = "anahtarlar"
            count = run.dataset_count if keyless else run.symbol_count
            typer.echo(
                f"run #{run.id}  {run.scope.value}  {run.status.value}  {run.started_at}  "
                f"{unit}={count} dataset={run.dataset_count} "
                f"fetched={run.rows_fetched} written={run.rows_written} "
                f"verified={run.rows_verified} skipped={run.rows_skipped}"
            )
            failures = list(
                session.execute(
                    select(SyncRunItem)
                    .where(SyncRunItem.run_id == run.id, SyncRunItem.status == "failed")
                    .limit(20)
                ).scalars()
            )
            for item in failures:
                typer.echo(f"    FAILED {item.symbol}/{item.dataset}: {item.error}")


@app.command("prune")
def prune(
    calendars_before: Annotated[
        str | None,
        typer.Option("--calendars-before", help="YYYY-MM-DD; eski takvim satirlarini siler"),
    ] = None,
    history_before: Annotated[
        str | None,
        typer.Option("--history-before", help="YYYY-MM-DD; eski _history anlik goruntuleri"),
    ] = None,
    asof_before: Annotated[
        str | None,
        typer.Option("--asof-before", help="YYYY-MM-DD; eski as-of satirlari (son gun korunur)"),
    ] = None,
    orphan_news: Annotated[
        bool, typer.Option("--orphan-news/--no-orphan-news", help="Oksuz haberleri sil")
    ] = True,
    orphan_reports: Annotated[
        bool,
        typer.Option(
            "--orphan-reports/--no-orphan-reports",
            help="Hicbir domain'e bagli olmayan analist raporlarini sil",
        ),
    ] = True,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Silmez, yalnizca sayar")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="YF_PRUNE_ENABLED=false iken de budar")
    ] = False,
) -> None:
    """Oksuz haberleri ve (istege bagli) eski takvim/_history/as-of satirlarini siler.

    Tarih sinirli budama VARSAYILAN OLARAK KAPALIDIR: `YF_PRUNE_ENABLED=true`
    ya da `--force` gerekir. Gerekce: takvim uclari pencere tabanlidir ve
    `_history` anlik goruntulerinin kaynakta karsiligi yoktur; silinen satir
    geri getirilemez.

    --asof-before her sembolun EN GUNCEL as-of gununu korur: as-of veri
    satiri silinse bile `asof_state` kapisi yerinde kalir ve bir sonraki
    kosu "degismedi" deyip hicbir sey yazmaz -- son gun silinseydi kayip
    kaynak hala veriyi verirken bile kalici olurdu (AH S5.4).
    """
    settings = get_settings()
    factory = _session_factory()
    try:
        with factory() as session:
            report = run_prune(
                session,
                enabled=settings.yf_prune_enabled or force,
                orphan_news=orphan_news,
                orphan_reports=orphan_reports,
                calendars_before=_parse_date(calendars_before),
                history_before=_parse_date(history_before),
                asof_before=_parse_date(asof_before),
                dry_run=dry_run,
            )
    except PruneDisabledError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    prefix = "silinecek" if report.dry_run else "silinen"
    typer.echo(f"{prefix} oksuz haber: {report.orphan_news}")
    typer.echo(f"{prefix} oksuz rapor: {report.orphan_reports}")
    for label, counts in (
        ("takvim", report.calendars),
        ("history", report.history),
        ("as-of", report.asof),
        ("domain as-of", report.domain_asof),
        ("kesif as-of", report.discovery_asof),
        ("ekran", report.screens),
    ):
        for table, count in sorted(counts.items()):
            typer.echo(f"{prefix} {label} [{table}]: {count}")
    typer.echo(f"toplam: {report.total}")


def _echo_tally(tally: Any) -> None:
    """Kosu ozeti -- `sync` ve `market sync`in yazdigi uc satirin aynisi."""
    typer.echo(f"run #{tally.run_id}  semboller={tally.symbol_count}")
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.counts.items())))
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in tally.totals.items()))


@discover_app.command("term")
def discover_term(
    query: str,
    datasets: Annotated[str, typer.Option("--datasets")] = "search,lookup",
) -> None:
    """Serbest terimle Search/Lookup kosar (SQ S8.2).

    Sembol dongusu KURMAZ: birkac terim icin shard makinesi anlamsiz
    olurdu; `market_runner` olceginde tek process calisir.
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    term = query.strip()
    if not term:
        typer.echo("bos terim", err=True)
        raise typer.Exit(code=1)
    if len(term) > QUERY_TERM_LENGTH or not term.isascii():
        typer.echo(
            f"terim en fazla {QUERY_TERM_LENGTH} ASCII karakter olabilir: {len(term)} karakter",
            err=True,
        )
        raise typer.Exit(code=1)

    selected = SYMBOL_DATASETS.resolve([d for d in datasets.split(",") if d.strip()])
    engine = create_db_engine(settings)
    try:
        summary = run_sync(engine, [term], selected, settings=settings, selector=f"term={term}")
    except LockNotAcquired:
        typer.echo("baska bir sync calisiyor (advisory lock alinamadi)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None
    _echo_tally(summary)
    raise typer.Exit(code=summary.exit_code())


@screen_app.command("sync")
def screen_sync(
    screens: Annotated[str | None, typer.Option("--screens")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages")] = None,
) -> None:
    """Ekranlari ceker (SQ S7.3).

    `yfin_market_sync` kilidini alir; sembol sync'i ile es zamanli kosar.
    """
    settings = get_settings()
    if screens:
        settings = settings.model_copy(update={"yf_screen_keys": screens})
    if max_pages is not None:
        settings = settings.model_copy(update={"yf_screen_max_pages": max_pages})
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = MARKET_DATASETS.resolve(["screener"])
    try:
        summary = run_market_sync(engine, selected, settings=settings)
    except LockNotAcquired:
        typer.echo("baska bir market sync calisiyor (advisory lock alinamadi)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None
    _echo_tally(summary)
    raise typer.Exit(code=summary.exit_code())


@screen_app.command("list")
def screen_list(
    discovered: Annotated[
        bool, typer.Option("--discovered", help="search_lists'ten kesfedilen ekranlar")
    ] = False,
) -> None:
    """Ekranlari ve son kosularini listeler.

    `total` != `fetched_rows` sayfa sinirina takilmayi SESSIZ DEGIL
    GORUNUR kilar (SQ S9.6/1).
    """
    factory = _session_factory()
    with factory() as session:
        if discovered:
            # SQ S8.3: `search_lists`in PREDEFINED_SCREENER satirlari,
            # `screens.py`de tanimli olmayan Yahoo ekran adlarini bildirir.
            # Otomatik KOSTURULMAZ; tabloya girmeleri operator kararidir.
            rows = session.execute(
                text(
                    "SELECT DISTINCT list_key, name, total FROM search_lists "
                    "WHERE list_type = 'PREDEFINED_SCREENER' "
                    "AND list_key NOT IN (SELECT screen_key FROM screens) "
                    "ORDER BY total DESC"
                )
            ).all()
            if not rows:
                typer.echo("kesfedilen yeni ekran yok")
                return
            for key, name, total in rows:
                typer.echo(f"{key:<32} {str(total or '-'):>8}  {name or ''}")
            return

        rows = session.execute(
            text(
                "SELECT s.screen_key, s.kind, s.quote_type, s.is_enabled, "
                "  r.as_of_date, r.total, r.fetched_rows, r.page_count "
                "FROM screens s LEFT JOIN screen_runs r "
                "  ON r.screen_key = s.screen_key "
                "  AND r.as_of_date = (SELECT MAX(as_of_date) FROM screen_runs "
                "                      WHERE screen_key = s.screen_key) "
                "ORDER BY s.screen_key"
            )
        ).all()

    if not rows:
        typer.echo("screens tablosu bos; `yfin screen sync` henuz kosmadi")
        return
    typer.echo(
        f"{'ekran':<28} {'tur':<11} {'tip':<11} {'akt':<5} "
        f"{'gun':<11} {'total':>7} {'alinan':>7} {'sayfa':>5}"
    )
    for key, kind, quote_type, enabled, as_of, total, fetched, pages in rows:
        flag = "evet" if enabled else "HAYIR"
        # `total` > `fetched_rows` -> ekran sayfa sinirina TAKILDI. Bu fark
        # eksiksizlik iddiasinin uc kanitindan biridir (SQ S9.6/1) ve
        # sessiz kalmamalidir.
        truncated = "  KIRPILDI" if total and fetched and total > fetched else ""
        typer.echo(
            f"{key:<28} {kind:<11} {quote_type:<11} {flag:<5} "
            f"{str(as_of or '-'):<11} {str(total or '-'):>7} {str(fetched or '-'):>7} "
            f"{str(pages or '-'):>5}{truncated}"
        )


@symbols_app.command("activate")
def symbols_activate(
    discovered_by: Annotated[
        str | None, typer.Option("--discovered-by", help="search | lookup | screener")
    ] = None,
    exchange: Annotated[str | None, typer.Option("--exchange")] = None,
    quote_type: Annotated[str | None, typer.Option("--quote-type")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Kesfedilmis pasif sembolleri aktiflestirir (SQ S8.3).

    `--dry-run` KASITLIDIR: `most_shorted_stocks` tek basina 4.022 sembol
    bildiriyor. Hepsini gormeden aktiflestirmek `yfin sync`in gunluk istek
    sayisini sessizce kat kat buyutebilir.
    """
    conditions: list[Any] = [Symbol.is_active.is_(False)]
    if discovered_by:
        conditions.append(Symbol.discovered_by == discovered_by)
    if exchange:
        conditions.append(Symbol.exchange == exchange.upper())
    if quote_type:
        conditions.append(Symbol.quote_type == quote_type.upper())

    factory = _session_factory()
    with factory() as session:
        rows = session.execute(
            select(Symbol.symbol, Symbol.exchange, Symbol.discovered_by)
            .where(*conditions)
            .order_by(Symbol.symbol)
        ).all()
        if not rows:
            typer.echo("olcute uyan pasif sembol yok")
            return
        if dry_run:
            for symbol, exch, source in rows:
                typer.echo(f"{symbol:<24} {exch or '-':<10} {source}")
            typer.echo(f"toplam: {len(rows)} (dry-run; hicbir sey degismedi)")
            return
        session.execute(update(Symbol).where(*conditions).values(is_active=True))
        session.commit()
    typer.echo(f"aktiflestirildi: {len(rows)}")


@market_app.command("sync")
def market_sync(
    datasets: Annotated[str, typer.Option("--datasets")] = "all",
    start: Annotated[str | None, typer.Option("--start", help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end", help="YYYY-MM-DD")] = None,
) -> None:
    """Piyasa (Market/Calendars) verisini ceker.

    Kendi advisory lock'unu (yfin_market_sync) kullanir; sembol sync'i ile
    es zamanli kosabilir.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = MARKET_DATASETS.resolve(None if datasets.strip() == "all" else datasets.split(","))
    # `_parse_day`: gecersiz tarihte anlamli mesaj + exit 1. Ham strptime
    # ValueError'i main()'in genel dalina duser ve kullanici
    # "beklenmeyen hata" gorurdu -- `sync` bunu zaten dogru yapiyordu.
    window_start = _parse_day(start, option="--start")
    window_end = _parse_day(end, option="--end")

    try:
        summary = run_market_sync(
            engine, selected, settings=settings, start=window_start, end=window_end
        )
    except LockNotAcquired:
        typer.echo("baska bir market sync calisiyor (advisory lock alinamadi)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None

    typer.echo(f"market run #{summary.run_id}  dataset={summary.dataset_count}")
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(summary.counts.items())))
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in summary.totals.items()))
    raise typer.Exit(code=summary.exit_code())


@market_app.command("datasets")
def market_datasets() -> None:
    """Piyasa dataset adlarini listeler."""
    typer.echo(", ".join(MARKET_DATASETS.user_visible_names()))


@domain_app.command("sync")
def domain_sync(
    datasets: Annotated[str, typer.Option("--datasets")] = "all",
    regions: Annotated[
        str | None,
        typer.Option("--regions", help="YF_DOMAIN_REGIONS'i ezer (or. US,GB)"),
    ] = None,
) -> None:
    """Sektor / endustri verisini ceker.

    Kendi advisory lock'unu (yfin_domain_sync) kullanir; sembol ve piyasa
    sync'leriyle es zamanli kosabilir.
    """
    settings = get_settings()
    if regions is not None:
        # `--regions` ezmesi Settings'in KOPYASI uzerinden yapilir:
        # `domain_regions()` yalniz `settings.yf_domain_regions` okur, bu
        # yuzden dogrulama (bicim + ampirik prob) CLI'dan gelen degere de
        # AYNEN uygulanir. `--shards`in YF_MAX_SHARDS'i ezmesiyle ayni
        # desen; fark, oradaki degerin dogrulanacak bir seyi olmamasi.
        settings = settings.model_copy(update={"yf_domain_regions": regions})
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = DOMAIN_DATASETS.resolve(None if datasets.strip() == "all" else datasets.split(","))

    try:
        summary = run_domain_sync(engine, selected, settings=settings)
    except RegionValidationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except LockNotAcquired:
        typer.echo("baska bir domain sync calisiyor (advisory lock alinamadi)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None

    typer.echo(f"domain run #{summary.run_id}  dataset={summary.dataset_count}")
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(summary.counts.items())))
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in summary.totals.items()))
    raise typer.Exit(code=summary.exit_code())


@domain_app.command("audit")
def domain_audit(
    as_of: Annotated[str | None, typer.Option("--as-of", help="YYYY-MM-DD")] = None,
    run: Annotated[int | None, typer.Option("--run", help="sync_runs.id")] = None,
) -> None:
    """Taksonomi eksiksizligini UC BAGIMSIZ kontrolle dogrular.

    Beklenen endustri sayisi BIZIM LISTEMIZDEN DEGIL, Yahoo'nun
    `overview.industriesCount` alanindan gelir. Basarisizlikta cikis kodu 1.
    """
    factory = _session_factory()
    day = _parse_day(as_of, option="--as-of")
    with factory() as session:
        report = audit_domains(session, as_of=day, run_id=run)

    typer.echo(f"sektor    : {report.sector_count}")
    typer.echo(
        f"endustri  : {report.industry_count} "
        f"(API'nin bildirdigi: {report.expected_industries})"
    )
    if report.cells_by_status:
        typer.echo("hucreler  : " + "  ".join(
            f"{k}={v}" for k, v in sorted(report.cells_by_status.items())
        ))
    for problem in report.problems:
        typer.echo(f"SORUN: {problem}", err=True)
    if not report.ok:
        raise typer.Exit(code=report.exit_code())
    typer.echo("audit ok")


@domain_app.command("datasets")
def domain_datasets() -> None:
    """Domain dataset adlarini listeler."""
    typer.echo(", ".join(DOMAIN_DATASETS.user_visible_names()))


@domain_app.command("list")
def domain_list(
    domain_type: Annotated[
        str | None, typer.Option("--type", help="sector | industry")
    ] = None,
    parent: Annotated[
        str | None, typer.Option("--parent", help="ebeveyn sektor anahtari")
    ] = None,
) -> None:
    """Kayitli sektor ve endustrileri listeler."""
    factory = _session_factory()
    stmt = select(Domain).order_by(Domain.domain_type, Domain.domain_key)
    if domain_type:
        stmt = stmt.where(Domain.domain_type == DomainType(domain_type.strip().lower()))
    if parent:
        stmt = stmt.where(Domain.parent_key == parent.strip())
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
    for row in rows:
        parent_label = row.parent_key or "-"
        typer.echo(
            f"{row.domain_type.value:9} {row.domain_key:40} {row.symbol:14} {parent_label}"
        )
    typer.echo(f"toplam: {len(rows)}")


@app.command("datasets")
def list_datasets() -> None:
    """Kullanilabilir dataset adlarini listeler (uc registry)."""
    typer.echo("sembol : " + ", ".join(SYMBOL_DATASETS.user_visible_names()))
    typer.echo("piyasa : " + ", ".join(MARKET_DATASETS.user_visible_names()))
    typer.echo("domain : " + ", ".join(DOMAIN_DATASETS.user_visible_names()))


def main() -> None:  # pragma: no cover
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("beklenmeyen hata", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------


def _proxy_by_label(session: Session, label: str) -> Proxy:
    row = session.execute(select(Proxy).where(Proxy.label == label)).scalar_one_or_none()
    if row is None:
        typer.echo(f"proxy bulunamadi: {label}", err=True)
        raise typer.Exit(code=1)
    return row


@proxy_app.command("add")
def proxy_add(
    url: Annotated[str, typer.Argument(help="scheme://[user:pass@]host:port")],
    label: Annotated[str | None, typer.Option("--label")] = None,
) -> None:
    """Havuza proxy ekler. Parola Fernet ile SIFRELENEREK saklanir."""
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        endpoint = px.parse_dsn(url)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if endpoint.scheme is ProxyScheme.HTTPS:
        # curl_cffi bu semada CurlCffiWarning uretir; kullanicilarin
        # cogu aslinda CONNECT-tunel icin http:// ister.
        typer.echo("uyari: 'https' proxy'ye TLS demektir; CONNECT-tunel icin 'http' kullanin")

    try:
        password_enc = px.encrypt_password(endpoint.password, settings)
    except px.SecretKeyMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    name = label or px.default_label(endpoint)
    factory = _session_factory()
    with factory() as session:
        existing = session.execute(
            select(Proxy).where(
                Proxy.scheme == endpoint.scheme,
                Proxy.host == endpoint.host,
                Proxy.port == endpoint.port,
                Proxy.username == endpoint.username,
            )
        ).scalar_one_or_none()
        if existing is not None:
            typer.echo(f"zaten var: {existing.label}")
            return
        session.add(
            Proxy(
                label=name,
                scheme=endpoint.scheme,
                host=endpoint.host,
                port=endpoint.port,
                username=endpoint.username,
                password_enc=password_enc,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # `label` UNIQUE'tir. Yukaridaki varlik kontrolu ENDPOINT'e
            # bakar, etikete degil: farkli bir endpoint ayni etiketle
            # eklenmek istendiginde ham SQLAlchemy traceback'i basardi.
            session.rollback()
            typer.echo(f"bu etiket zaten kullanimda: {name}", err=True)
            raise typer.Exit(code=1) from None
    typer.echo(f"eklendi: {name} ({endpoint.scheme.value}://{endpoint.host}:{endpoint.port})")


@proxy_app.command("list")
def proxy_list(
    show_all: Annotated[bool, typer.Option("--all", help="Devre disi olanlari da goster")] = False,
) -> None:
    """Havuzu listeler. PAROLA HICBIR ZAMAN BASILMAZ."""
    factory = _session_factory()
    now = datetime.now(UTC)
    stmt = select(Proxy).order_by(Proxy.label)
    if not show_all:
        stmt = stmt.where(Proxy.is_enabled.is_(True))
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
    if not rows:
        typer.echo("proxy yok")
        return
    typer.echo(f"{'LABEL':<20} {'ENDPOINT':<28} {'ON':<3} {'HEALTH':<18} {'OK/FAIL':<12} LATENCY")
    for row in rows:
        health = row.health.value
        if row.health is ProxyHealth.COOLDOWN and row.cooldown_until and row.cooldown_until <= now:
            # ENUM'un yalan soylemesi operatore yansimasin: cooldown suresi
            # dolmustur ve proxy yeniden UYGUNDUR, ama ilk basariya kadar
            # health 'cooldown' kalir.
            health = "cooldown (expired)"
        latency = f"{row.last_latency_ms} ms" if row.last_latency_ms is not None else "-"
        typer.echo(
            f"{row.label:<20} {f'{row.scheme.value}://{row.endpoint()}':<28} "
            f"{'1' if row.is_enabled else '0':<3} {health:<18} "
            f"{f'{row.success_count}/{row.failure_count}':<12} {latency}"
        )


@proxy_app.command("enable")
def proxy_enable(label: str) -> None:
    """Operatör karari: havuza geri al (health'e DOKUNMAZ)."""
    _set_enabled(label, True)


@proxy_app.command("disable")
def proxy_disable(label: str) -> None:
    """Operatör karari: havuzdan cikar (health'e DOKUNMAZ)."""
    _set_enabled(label, False)


def _set_enabled(label: str, value: bool) -> None:
    factory = _session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(update(Proxy).where(Proxy.id == row.id).values(is_enabled=value))
        session.commit()
    typer.echo(f"{'etkin' if value else 'devre disi'}: {label}")


@proxy_app.command("reset")
def proxy_reset(label: str) -> None:
    """Saglik durumunu sifirlar; dead bir proxy'yi geri getirir.

    KUMULATIF success_count/failure_count KORUNUR: gecmis bilgi silinmez.
    """
    factory = _session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(
            update(Proxy)
            .where(Proxy.id == row.id)
            .values(
                health=ProxyHealth.UNKNOWN,
                consecutive_failures=0,
                cooldown_rounds=0,
                cooldown_until=None,
                last_error=None,
            )
        )
        session.commit()
    typer.echo(f"sifirlandi: {label}")


@proxy_app.command("remove")
def proxy_remove(label: str) -> None:
    """Havuzdan siler. sync_run_items.proxy_label kopyasi KALIR."""
    factory = _session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(delete(Proxy).where(Proxy.id == row.id))
        session.commit()
    typer.echo(f"silindi: {label}")


@proxy_app.command("check")
def proxy_check(
    label: Annotated[str | None, typer.Option("--label", help="Yalnizca bu proxy")] = None,
) -> None:
    """Her proxy'yi Yahoo'ya karsi dogrular ve sagligini tazeler.

    yfinance KULLANILMAZ (yf.config process-global oldugu icin N proxy'yi
    paralel kontrol etmek N process gerektirirdi); ham curl_cffi istegi
    atilir. Iki endpoint denenir: chart crumb istemez ama gercek trafik
    /v1/test/getcrumb'dan da gecer.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    policy = px.ProxyPolicy.from_settings(settings)
    factory = _session_factory()

    stmt = select(Proxy).order_by(Proxy.label)
    if label:
        stmt = stmt.where(Proxy.label == label)
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
        targets: list[tuple[int, str, px.ProxyEndpoint | None, str]] = []
        for row in rows:
            try:
                targets.append((int(row.id), row.label, px.endpoint_of(row, settings), ""))
            except px.PasswordUndecryptable as exc:
                # Durum DEGISTIRILMEZ: yanlis teshis uretmemek icin.
                targets.append((int(row.id), row.label, None, str(exc)))

    if not targets:
        typer.echo("proxy yok")
        return

    def _run(target: tuple[int, str, px.ProxyEndpoint | None, str]) -> tuple[int, str, Any]:
        proxy_id, name, endpoint, error = target
        if endpoint is None:
            return proxy_id, name, px.CheckResult(name, None, None, error)
        return proxy_id, name, px.check_endpoint(endpoint, settings.yf_proxy_check_timeout)

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        outcomes = list(pool.map(_run, targets))

    with factory() as session:
        for proxy_id, name, result in outcomes:
            if result.event is None:
                typer.echo(f"{name:<20} ATLANDI  {result.detail}")
                continue
            px.persist_event(
                session,
                proxy_id,
                result.event,
                policy=policy,
                error=result.detail or None,
                latency_ms=result.latency_ms,
                checked=True,
            )
            latency = f"{result.latency_ms} ms" if result.latency_ms is not None else "-"
            typer.echo(f"{name:<20} {result.event.value:<14} {latency}  {result.detail}")
        session.commit()
