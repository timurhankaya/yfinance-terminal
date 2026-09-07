"""Command-line interface."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Annotated, Any

import typer
from sqlalchemy import Engine, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from yfin import proxy as px
from yfin.cli.api import api_app
from yfin.cli.bars import bars_app, scope_app
from yfin.cli.settings import config_app
from yfin.core import normalize as nz
from yfin.core.config import SETTINGS_SOURCE_VAR, bootstrap_settings, get_settings
from yfin.core.logging_setup import configure_logging, get_logger
from yfin.datasets import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS
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
from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED, EXIT_NO_PROXY
from yfin.pipeline.domain_audit import audit_domains
from yfin.pipeline.domain_runner import RegionValidationError, run_domain_sync
from yfin.pipeline.market_runner import run_market_sync
from yfin.pipeline.prune import PruneDisabledError, run_prune
from yfin.pipeline.runner import run_sync
from yfin.pipeline.shard import NoEligibleProxy, run_sharded
from yfin.storage.db import LockNotAcquired, create_db_engine

log = get_logger(__name__)

app = typer.Typer(
    help="yfinance -> PostgreSQL 18 + TimescaleDB data pipeline", no_args_is_help=True
)
db_app = typer.Typer(help="Database operations", no_args_is_help=True)
symbols_app = typer.Typer(help="Symbol universe management", no_args_is_help=True)
proxy_app = typer.Typer(help="Proxy pool management", no_args_is_help=True)
app.add_typer(db_app, name="db")
market_app = typer.Typer(help="Market (Market/Calendars) data", no_args_is_help=True)
screen_app = typer.Typer(help="Screener screens", no_args_is_help=True)
discover_app = typer.Typer(help="Free-text term discovery", no_args_is_help=True)
domain_app = typer.Typer(help="Sector / industry data", no_args_is_help=True)
app.add_typer(symbols_app, name="symbols")
app.add_typer(proxy_app, name="proxy")
app.add_typer(market_app, name="market")
app.add_typer(screen_app, name="screen")
app.add_typer(discover_app, name="discover")
app.add_typer(domain_app, name="domain")
# price_bars commands live in their own module: cli.py was already 630 lines and
# none of them share state with the existing commands.
app.add_typer(bars_app, name="bars")
app.add_typer(scope_app, name="scope")
# DB-backed configuration lives in its own module: none of its commands share
# state with the existing ones, and cli.py had already passed 1000 lines.
app.add_typer(config_app, name="config")
app.add_typer(api_app, name="api")


def _parse_date(value: str | None) -> datetime | None:
    """YYYY-MM-DD -> UTC-aware datetime.

    Columns are timestamptz. A naive bound would be interpreted by psycopg using
    the connection's timezone -- the value comes out correct but compares at a
    different awareness level than the column.
    """
    return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC) if value else None


def _parse_day(value: str | None, *, option: str) -> date | None:
    """YYYY-MM-DD -> date. An invalid value never silently becomes None: a wrong
    range would otherwise look like it was applied."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        typer.echo(f"invalid date for {option}: {value} (expected YYYY-MM-DD)", err=True)
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
    """Records the run's symbol universe and date range in human-readable form.

    The `scope` column only carries the symbols/market split; without this,
    which universe a given run covered would be unknowable in retrospect and
    any completeness claim would be unverifiable.
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
    """Converts --exchange / --quote-type input to match the write path's casing.

    `datasets/symbols.py` writes these two columns with `.upper()`; if the
    filter did not match that casing, `--exchange nms` would silently return
    an empty result.
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
    """--exchange / --quote-type / --suffix filters, AND-ed together.

    Input is normalized with `.upper()`. Columns are COLLATE "C" (case
    sensitive) and the write path also upper-cases (datasets/symbols.py), so
    both sides meet in the same casing.

    `func.upper` is still avoided, though the reason changed: it used to be
    unnecessary because the column was case-insensitive; now wrapping the
    column in a function would make the `ix_symbols_exchange` index unusable.
    Normalization happens on the input, not the column.
    """
    stmt = base_stmt
    if exchanges:
        stmt = stmt.where(Symbol.exchange.in_(_normalize_filter_values(exchanges)))
    if quote_types:
        stmt = stmt.where(Symbol.quote_type.in_(_normalize_filter_values(quote_types)))
    if suffix:
        # Works even before the exchange is resolved; does not hit the NULL trap.
        stmt = stmt.where(Symbol.symbol.like(f"%{suffix.strip().upper()}"))
    codes = list(session.execute(stmt).scalars())

    # NULL trap: the bootstrap `symbols` dataset fills the exchange/quote_type
    # columns; a symbol added via `yfin symbols add` has them NULL until its
    # first sync, and the filter would silently exclude it.
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
                f"{len(unresolved)} symbols left out by the filter "
                f"({names} NULL - not resolved yet)."
            )
            typer.echo(
                "Run 'yfin sync --datasets symbols' first, or use --suffix."
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
    revision: Annotated[str, typer.Argument(help="Target revision")] = "head",
) -> None:
    """Applies Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    configure_logging(get_settings().log_level)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, revision)
    typer.echo(f"migration uygulandi: {revision}")
    _warn_missing_settings_rows()


def _warn_missing_settings_rows() -> None:
    """Warns in one line about DB-managed keys that have no settings row.

    After migrating to the DB-backed layer, the `.env` layer is effectively
    empty; if `yfin config seed` is not run for a field newly added to
    `Settings`, its value falls through to the model default instead of
    `.env`. `seed` is therefore a standard step after every `upgrade`, and
    this command reminds the operator of that.

    This reads `Settings.model_fields` from a command, not a migration, so it
    does not violate the rule against migrations importing application code.
    """
    from yfin.core.config import DB_MANAGED_FIELDS, bootstrap_settings, source_is_env
    from yfin.storage.settings_store import fetch_rows

    if source_is_env():
        return
    try:
        rows = fetch_rows(bootstrap_settings())
    except Exception as exc:  # noqa: BLE001 - warning path, must not crash the command
        typer.echo(
            f"could not read the settings table, skipping the missing-row check: {exc}",
            err=True,
        )
        return
    if rows is None:
        return
    missing = sorted(DB_MANAGED_FIELDS - set(rows))
    if missing:
        typer.echo(
            f"{len(missing)} settings have no `settings` row (first: {missing[0]}); "
            "their values will come from .env or the model default. "
            "Run `yfin config seed`."
        )


@db_app.command("create")
def db_create() -> None:
    """Creates the database if it does not exist.

    Never touches the DB-backed configuration layer: the command that creates
    the database cannot connect to a database that does not exist yet, but
    `get_settings()` would try to connect to exactly that database to read
    the `settings` table.
    """
    from sqlalchemy import create_engine

    settings = bootstrap_settings()
    # AUTOCOMMIT is required: PostgreSQL rejects `CREATE DATABASE` inside a
    # transaction block.
    engine = create_engine(settings.bootstrap_url(), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        for name in (settings.db_name, settings.db_test_name):
            # PostgreSQL has no `CREATE DATABASE IF NOT EXISTS`; existence is
            # checked via pg_database instead.
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()

    # The extension is installed separately per database: `CREATE EXTENSION`
    # is database-scoped. It does not go in a migration -- that would break
    # symmetry with `downgrade`, and the extension is a product of `db create`.
    for name in (settings.db_name, settings.db_test_name):
        db_engine = create_engine(settings.db_url(name), isolation_level="AUTOCOMMIT")
        with db_engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        db_engine.dispose()

    typer.echo(f"veritabani hazir: {settings.db_name}, {settings.db_test_name}")


@db_app.command("revision")
def db_revision(message: Annotated[str, typer.Option("-m", "--message")]) -> None:
    """Generates a new migration from the models.

    Disables the DB-backed configuration layer. `migrations/env.py` calls
    `get_settings()`; a `revision` run before the schema exists would
    otherwise fail looking for the `settings` table. The env var has to be
    set directly, since the key that disables the layer cannot itself be
    read from the layer (chicken-and-egg).
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
    symbols: Annotated[list[str], typer.Argument(help="Symbol codes")],
) -> None:
    """Adds symbols. strip().upper() is applied."""
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
        typer.echo(f"added: {', '.join(added)}")
    if existing:
        typer.echo(f"already present: {', '.join(existing)}")


@symbols_app.command("list")
def symbols_list(
    include_inactive: Annotated[bool, typer.Option("--include-inactive")] = False,
) -> None:
    """Lists the symbol universe."""
    factory = _session_factory()
    stmt = select(Symbol).order_by(Symbol.symbol)
    if not include_inactive:
        stmt = stmt.where(Symbol.is_active.is_(True))
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
    if not rows:
        typer.echo("no symbols")
        return
    for row in rows:
        flag = "" if row.is_active else " [inactive]"
        typer.echo(f"{row.symbol:<12} {row.quote_type or '-':<10} {row.short_name or ''}{flag}")


@symbols_app.command("exchanges")
def symbols_exchanges(
    include_inactive: Annotated[bool, typer.Option("--include-inactive")] = False,
) -> None:
    """Lists (exchange, full name, type) triples in the universe with counts.

    Exists so --exchange/--quote-type values are discoverable; the NULL row
    surfaces symbols that are not yet resolved.
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
        typer.echo("no symbols")
        return
    for exchange, full_name, quote_type, count in rows:
        code = exchange or "NULL (cozulmemis)"
        typer.echo(f"{code:<20} {quote_type or '-':<16} {full_name or '':<32} {count}")


@symbols_app.command("deactivate")
def symbols_deactivate(symbol: str) -> None:
    """Soft delete: is_active=0. No data is deleted."""
    code = nz.normalize_symbol(symbol)
    factory = _session_factory()
    with factory() as session:
        result = session.execute(
            update(Symbol).where(Symbol.symbol == code).values(is_active=False)
        )
        changed = result.rowcount  # type: ignore[attr-defined]
        session.commit()
    typer.echo(f"deactivated: {code}" if changed else f"not found: {code}")


@symbols_app.command("purge")
def symbols_purge(
    symbol: str,
    force: Annotated[bool, typer.Option("--force", help="Hard delete; data loss")] = False,
) -> None:
    """Hard delete. Related rows are deleted explicitly, in order, because of
    ON DELETE RESTRICT."""
    code = nz.normalize_symbol(symbol)
    if not force:
        typer.echo("this command is irreversible; pass --force to confirm")
        raise typer.Exit(code=1)

    factory = _session_factory()
    with factory() as session:
        for name in symbol_scoped_tables():
            table = Base.metadata.tables[name]
            session.execute(delete(table).where(table.c["symbol"] == code))
        session.execute(delete(NewsSymbol).where(NewsSymbol.symbol == code))
        session.execute(delete(Symbol).where(Symbol.symbol == code))
        session.commit()
    typer.echo(f"deleted: {code}")


# --------------------------------------------------------------------------
# sync / status / prune
# --------------------------------------------------------------------------


@app.command("sync")
def sync(
    symbols: Annotated[str | None, typer.Option("--symbols", help="Comma-separated list")] = None,
    datasets: Annotated[str, typer.Option("--datasets")] = "all",
    full_refresh: Annotated[bool, typer.Option("--full-refresh")] = False,
    include_inactive: Annotated[bool, typer.Option("--include-inactive")] = False,
    shards: Annotated[
        int | None, typer.Option("--shards", help="Temporarily overrides YF_MAX_SHARDS")
    ] = None,
    no_proxy: Annotated[
        bool, typer.Option("--no-proxy", help="Ignore the pool; single shard, direct connection")
    ] = False,
    require_proxy: Annotated[
        bool, typer.Option("--require-proxy", help="Do not run if no eligible proxy")
    ] = False,
    start: Annotated[str | None, typer.Option("--start", help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end", help="YYYY-MM-DD")] = None,
    exchange: Annotated[
        str | None, typer.Option("--exchange", help="Comma-separated exchange code (NMS,NYQ,IST)")
    ] = None,
    quote_type: Annotated[
        str | None, typer.Option("--quote-type", help="Comma-separated type (EQUITY,ETF)")
    ] = None,
    suffix: Annotated[
        str | None,
        typer.Option("--suffix", help="Symbol suffix (.IS) -- works before exchange is resolved"),
    ] = None,
) -> None:
    """Fetches data and writes it to PostgreSQL.

    If the proxy pool has eligible proxies, the run is split into shards: one
    OS process per proxy. History repair can rewrite the past, so getting all
    corrections down requires a periodic --full-refresh.

    --start/--end does a real backward fetch on datasets with
    `date_range="api"`, filters rows on `"filter"` datasets, and is not
    applied at all on `"none"` datasets.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = SYMBOL_DATASETS.resolve(None if datasets.strip() == "all" else datasets.split(","))

    start_date = _parse_day(start, option="--start")
    end_date = _parse_day(end, option="--end")
    if start_date is not None and end_date is not None and start_date > end_date:
        typer.echo("--start cannot be later than --end", err=True)
        raise typer.Exit(code=1)

    exchanges = _csv_upper(exchange)
    quote_types = _csv_upper(quote_type)
    filtered = bool(exchanges or quote_types or suffix)

    if symbols and filtered:
        # If both were given, which one wins would be a silent assumption; the
        # user must either enumerate the universe explicitly or filter it.
        typer.echo("--symbols cannot be combined with --exchange/--quote-type/--suffix", err=True)
        raise typer.Exit(code=1)

    if (start_date is not None or end_date is not None) and all(
        d.date_range == "none" for d in selected if d.name != SYMBOL_DATASETS.bootstrap
    ):
        typer.echo(
            "none of the selected datasets supports a date range; "
            "drop --start/--end, or pick a dataset that does, such as "
            "'history' or 'upgrades_downgrades'",
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
        typer.echo("no symbols; run 'yfin symbols add ...' first")
        raise typer.Exit(code=0)

    try:
        # The lock is taken by the coordinator; children do not take a lock.
        # A delayed cron trigger cannot stack on top of a running sync.
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
        typer.echo("another sync is running (advisory lock not acquired)", err=True)
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
    """Summary of the most recent sync run."""
    factory = _session_factory()
    with factory() as session:
        stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
        if scope:
            stmt = select(SyncRun).where(SyncRun.scope == RunScope(scope.strip().lower()))
            stmt = stmt.order_by(SyncRun.started_at.desc()).limit(limit)
        runs = list(session.execute(stmt).scalars())
        if not runs:
            typer.echo("no sync has been run yet")
            return
        for run in runs:
            # A market run has no "symbol"; region count is not written into
            # symbol_count, so the label changes too
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
        typer.Option("--calendars-before", help="YYYY-MM-DD; deletes old calendar rows"),
    ] = None,
    history_before: Annotated[
        str | None,
        typer.Option("--history-before", help="YYYY-MM-DD; old _history snapshots"),
    ] = None,
    asof_before: Annotated[
        str | None,
        typer.Option("--asof-before", help="YYYY-MM-DD; old as-of rows (last day kept)"),
    ] = None,
    orphan_news: Annotated[
        bool, typer.Option("--orphan-news/--no-orphan-news", help="Delete orphaned news")
    ] = True,
    orphan_reports: Annotated[
        bool,
        typer.Option(
            "--orphan-reports/--no-orphan-reports",
            help="Delete analyst reports not linked to any domain",
        ),
    ] = True,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do not delete, only count")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Prune even when YF_PRUNE_ENABLED=false")
    ] = False,
) -> None:
    """Deletes orphaned news and, optionally, old calendar/_history/as-of rows.

    Date-bounded pruning is disabled by default: it needs `YF_PRUNE_ENABLED=true`
    or `--force`. Reason: calendar ends are window-based and `_history` snapshots
    have no source-of-truth counterpart, so a deleted row cannot be recovered.

    --asof-before always keeps each symbol's most recent as-of day: even if
    that row's data is deleted, the `asof_state` gate stays in place, so the
    next run says "unchanged" and writes nothing -- if the last day were
    deleted, that gap would persist even while the source still serves the
    data.
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

    prefix = "to delete" if report.dry_run else "deleted"
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
    typer.echo(f"total: {report.total}")


def _echo_tally(tally: Any) -> None:
    """Run summary -- the same three lines `sync` and `market sync` print."""
    typer.echo(f"run #{tally.run_id}  symbols={tally.symbol_count}")
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.counts.items())))
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in tally.totals.items()))


@discover_app.command("term")
def discover_term(
    query: str,
    datasets: Annotated[str, typer.Option("--datasets")] = "search,lookup",
) -> None:
    """Runs Search/Lookup with a free-text term.

    Does not loop over symbols: the shard machinery would be pointless for a
    handful of terms, so it runs as a single process at `market_runner` scale.
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    term = query.strip()
    if not term:
        typer.echo("empty term", err=True)
        raise typer.Exit(code=1)
    if len(term) > QUERY_TERM_LENGTH or not term.isascii():
        typer.echo(
            f"a term may be at most {QUERY_TERM_LENGTH} ASCII characters: {len(term)} given",
            err=True,
        )
        raise typer.Exit(code=1)

    selected = SYMBOL_DATASETS.resolve([d for d in datasets.split(",") if d.strip()])
    engine = create_db_engine(settings)
    try:
        summary = run_sync(engine, [term], selected, settings=settings, selector=f"term={term}")
    except LockNotAcquired:
        typer.echo("another sync is running (advisory lock not acquired)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None
    _echo_tally(summary)
    raise typer.Exit(code=summary.exit_code())


@screen_app.command("sync")
def screen_sync(
    screens: Annotated[str | None, typer.Option("--screens")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages")] = None,
) -> None:
    """Fetches screens.

    Takes the `yfin_market_sync` lock; can run concurrently with symbol sync.
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
        typer.echo("another market sync is running (advisory lock not acquired)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None
    _echo_tally(summary)
    raise typer.Exit(code=summary.exit_code())


@screen_app.command("list")
def screen_list(
    discovered: Annotated[
        bool, typer.Option("--discovered", help="Screens discovered from search_lists")
    ] = False,
) -> None:
    """Lists screens and their most recent runs.

    `total` != `fetched_rows` makes hitting the page limit visible instead of
    silent.
    """
    factory = _session_factory()
    with factory() as session:
        if discovered:
            # The PREDEFINED_SCREENER rows of `search_lists` report Yahoo screen
            # names not defined in `screens.py`. Never run automatically; adding
            # them to the table is an operator decision.
            rows = session.execute(
                text(
                    "SELECT DISTINCT list_key, name, total FROM search_lists "
                    "WHERE list_type = 'PREDEFINED_SCREENER' "
                    "AND list_key NOT IN (SELECT screen_key FROM screens) "
                    "ORDER BY total DESC"
                )
            ).all()
            if not rows:
                typer.echo("no new screens discovered")
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
        typer.echo("screens table is empty; `yfin screen sync` has not run yet")
        return
    typer.echo(
        f"{'ekran':<28} {'tur':<11} {'tip':<11} {'akt':<5} "
        f"{'gun':<11} {'total':>7} {'alinan':>7} {'sayfa':>5}"
    )
    for key, kind, quote_type, enabled, as_of, total, fetched, pages in rows:
        flag = "evet" if enabled else "HAYIR"
        # `total` > `fetched_rows` -> the screen hit the page limit. This gap is
        # one of the edge-case proofs of a completeness claim and must not be
        # silent.
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
    """Activates discovered, inactive symbols.

    `--dry-run` is deliberate: `most_shorted_stocks` alone reports 4,022
    symbols. Activating them without a preview could silently multiply
    `yfin sync`'s daily request count.
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
            typer.echo("no inactive symbols match the criteria")
            return
        if dry_run:
            for symbol, exch, source in rows:
                typer.echo(f"{symbol:<24} {exch or '-':<10} {source}")
            typer.echo(f"total: {len(rows)} (dry-run; nothing changed)")
            return
        session.execute(update(Symbol).where(*conditions).values(is_active=True))
        session.commit()
    typer.echo(f"activated: {len(rows)}")


@market_app.command("sync")
def market_sync(
    datasets: Annotated[str, typer.Option("--datasets")] = "all",
    start: Annotated[str | None, typer.Option("--start", help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end", help="YYYY-MM-DD")] = None,
) -> None:
    """Fetches market (Market/Calendars) data.

    Uses its own advisory lock (yfin_market_sync); can run concurrently with
    symbol sync.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = MARKET_DATASETS.resolve(None if datasets.strip() == "all" else datasets.split(","))
    # `_parse_day` gives a meaningful message + exit 1 on an invalid date. A raw
    # strptime ValueError would fall through to main()'s generic handler and
    # show the user "unexpected error" -- `sync` already did this correctly.
    window_start = _parse_day(start, option="--start")
    window_end = _parse_day(end, option="--end")

    try:
        summary = run_market_sync(
            engine, selected, settings=settings, start=window_start, end=window_end
        )
    except LockNotAcquired:
        typer.echo("another market sync is running (advisory lock not acquired)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None

    typer.echo(f"market run #{summary.run_id}  dataset={summary.dataset_count}")
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(summary.counts.items())))
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in summary.totals.items()))
    raise typer.Exit(code=summary.exit_code())


@market_app.command("datasets")
def market_datasets() -> None:
    """Lists market dataset names."""
    typer.echo(", ".join(MARKET_DATASETS.user_visible_names()))


@domain_app.command("sync")
def domain_sync(
    datasets: Annotated[str, typer.Option("--datasets")] = "all",
    regions: Annotated[
        str | None,
        typer.Option("--regions", help="Overrides YF_DOMAIN_REGIONS (e.g. US,GB)"),
    ] = None,
) -> None:
    """Fetches sector / industry data.

    Uses its own advisory lock (yfin_domain_sync); can run concurrently with
    symbol and market sync.
    """
    settings = get_settings()
    if regions is not None:
        # The `--regions` override goes through a copy of Settings:
        # `domain_regions()` only reads `settings.yf_domain_regions`, so
        # validation (format + empirical probe) applies to the CLI value the
        # same way. Same pattern as `--shards` overriding YF_MAX_SHARDS; the
        # difference is that value has nothing to validate.
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
        typer.echo("another domain sync is running (advisory lock not acquired)", err=True)
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
    """Verifies taxonomy completeness with three independent checks.

    The expected industry count comes from Yahoo's `overview.industriesCount`
    field, not from our own list. Exit code 1 on failure.
    """
    factory = _session_factory()
    day = _parse_day(as_of, option="--as-of")
    with factory() as session:
        report = audit_domains(session, as_of=day, run_id=run)

    typer.echo(f"sectors   : {report.sector_count}")
    typer.echo(
        f"industries: {report.industry_count} "
        f"(the API reports: {report.expected_industries})"
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
    """Lists domain dataset names."""
    typer.echo(", ".join(DOMAIN_DATASETS.user_visible_names()))


@domain_app.command("list")
def domain_list(
    domain_type: Annotated[
        str | None, typer.Option("--type", help="sector | industry")
    ] = None,
    parent: Annotated[
        str | None, typer.Option("--parent", help="parent sector key")
    ] = None,
) -> None:
    """Lists registered sectors and industries."""
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
    typer.echo(f"total: {len(rows)}")


@app.command("datasets")
def list_datasets() -> None:
    """Lists available dataset names (all three registries)."""
    typer.echo("symbol : " + ", ".join(SYMBOL_DATASETS.user_visible_names()))
    typer.echo("market : " + ", ".join(MARKET_DATASETS.user_visible_names()))
    typer.echo("domain : " + ", ".join(DOMAIN_DATASETS.user_visible_names()))


def main() -> None:  # pragma: no cover
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("unexpected error", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------


def _proxy_by_label(session: Session, label: str) -> Proxy:
    row = session.execute(select(Proxy).where(Proxy.label == label)).scalar_one_or_none()
    if row is None:
        typer.echo(f"proxy not found: {label}", err=True)
        raise typer.Exit(code=1)
    return row


@proxy_app.command("add")
def proxy_add(
    url: Annotated[str, typer.Argument(help="scheme://[user:pass@]host:port")],
    label: Annotated[str | None, typer.Option("--label")] = None,
) -> None:
    """Adds a proxy to the pool. The password is stored encrypted with Fernet."""
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        endpoint = px.parse_dsn(url)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if endpoint.scheme is ProxyScheme.HTTPS:
        # curl_cffi emits a CurlCffiWarning for this scheme; most users actually
        # want http:// for a CONNECT tunnel.
        typer.echo("warning: 'https' means TLS to the proxy; use 'http' for a CONNECT tunnel")

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
            typer.echo(f"already present: {existing.label}")
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
            # `label` is UNIQUE. The existence check above looks at the endpoint,
            # not the label: adding a different endpoint under the same label
            # used to print a raw SQLAlchemy traceback.
            session.rollback()
            typer.echo(f"that label is already in use: {name}", err=True)
            raise typer.Exit(code=1) from None
    typer.echo(f"added: {name} ({endpoint.scheme.value}://{endpoint.host}:{endpoint.port})")


@proxy_app.command("list")
def proxy_list(
    show_all: Annotated[bool, typer.Option("--all", help="Also show disabled proxies")] = False,
) -> None:
    """Lists the pool. The password is never printed."""
    factory = _session_factory()
    now = datetime.now(UTC)
    stmt = select(Proxy).order_by(Proxy.label)
    if not show_all:
        stmt = stmt.where(Proxy.is_enabled.is_(True))
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
    if not rows:
        typer.echo("no proxies")
        return
    typer.echo(f"{'LABEL':<20} {'ENDPOINT':<28} {'ON':<3} {'HEALTH':<18} {'OK/FAIL':<12} LATENCY")
    for row in rows:
        health = row.health.value
        if row.health is ProxyHealth.COOLDOWN and row.cooldown_until and row.cooldown_until <= now:
            # Don't let the stale enum mislead the operator: the cooldown has
            # expired and the proxy is eligible again, but health stays
            # 'cooldown' until the next success.
            health = "cooldown (expired)"
        latency = f"{row.last_latency_ms} ms" if row.last_latency_ms is not None else "-"
        typer.echo(
            f"{row.label:<20} {f'{row.scheme.value}://{row.endpoint()}':<28} "
            f"{'1' if row.is_enabled else '0':<3} {health:<18} "
            f"{f'{row.success_count}/{row.failure_count}':<12} {latency}"
        )


@proxy_app.command("enable")
def proxy_enable(label: str) -> None:
    """Operator decision: re-add to the pool (does not touch health)."""
    _set_enabled(label, True)


@proxy_app.command("disable")
def proxy_disable(label: str) -> None:
    """Operator decision: remove from the pool (does not touch health)."""
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
    """Resets health state; brings a dead proxy back.

    Cumulative success_count/failure_count are preserved: past data is not
    deleted.
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
    """Deletes from the pool. The copy in sync_run_items.proxy_label remains."""
    factory = _session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(delete(Proxy).where(Proxy.id == row.id))
        session.commit()
    typer.echo(f"deleted: {label}")


@proxy_app.command("check")
def proxy_check(
    label: Annotated[str | None, typer.Option("--label", help="Only this proxy")] = None,
) -> None:
    """Verifies each proxy against Yahoo and refreshes its health.

    Does not use yfinance: yf.config is process-global, so checking N proxies
    in parallel would need N processes. A raw curl_cffi request is sent
    instead. Two endpoints are tried: chart does not require a crumb, but
    real traffic also goes through /v1/test/getcrumb.
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
                # State is not changed, to avoid producing a false diagnosis.
                targets.append((int(row.id), row.label, None, str(exc)))

    if not targets:
        typer.echo("no proxies")
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
