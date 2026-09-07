"""Command-line interface: the top-level commands, and the assembly.

What is here is what belongs to no group -- `sync`, `status`, `prune`,
`datasets` -- plus the wiring that mounts the groups. Each group lives in
its own module, which is the pattern `bars`, `settings`, `stream` and `api`
already followed; `db`, `symbols`, `proxy`, `market`, `screen`,
`discover` and `domain` were the seven that had not been moved yet, and
this file was 1249 lines because of it.

The heavy imports -- pipeline, datasets, models -- are made INSIDE the
command bodies. At module level they made `yfin --help` build the whole
dataset registry and import SQLAlchemy's model package, on every
invocation, to print a list of command names. `cli/stream.py` already did
it this way.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from yfin.cli.api import api_app
from yfin.cli.bars import bars_app, scope_app
from yfin.cli.changes import changes_app
from yfin.cli.common import (
    echo_tally,
    filtered_symbols,
    parse_date,
    parse_day,
    selector,
    session_factory,
)
from yfin.cli.db import db_app
from yfin.cli.domain import domain_app
from yfin.cli.market import market_app, screen_app
from yfin.cli.proxy import proxy_app
from yfin.cli.settings import config_app
from yfin.cli.stream import stream_app
from yfin.cli.symbols import discover_app, symbols_app
from yfin.core.config import get_settings
from yfin.core.logging_setup import configure_logging, get_logger
from yfin.core.text import comma_list

log = get_logger(__name__)

app = typer.Typer(
    help="yfinance -> PostgreSQL 18 + TimescaleDB data pipeline", no_args_is_help=True
)
app.add_typer(db_app, name="db")
app.add_typer(symbols_app, name="symbols")
app.add_typer(proxy_app, name="proxy")
app.add_typer(market_app, name="market")
app.add_typer(screen_app, name="screen")
app.add_typer(discover_app, name="discover")
app.add_typer(domain_app, name="domain")
app.add_typer(bars_app, name="bars")
app.add_typer(stream_app, name="stream")
app.add_typer(changes_app, name="changes")
app.add_typer(scope_app, name="scope")
app.add_typer(config_app, name="config")
app.add_typer(api_app, name="api")


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
    # The pipeline, the registry and the models package are imported here,
    # not at module level. This module is what `yfin` runs, so a top-level
    # import made `yfin --help` build the whole dataset registry to print a
    # list of command names.
    from sqlalchemy import select

    from yfin.core import normalize as nz
    from yfin.datasets import SYMBOL_DATASETS
    from yfin.models import Symbol
    from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED, EXIT_NO_PROXY
    from yfin.pipeline.proxy_plan import NoEligibleProxy
    from yfin.pipeline.shard import run_sharded
    from yfin.storage.db import (
        LockNotAcquired,
        create_db_engine,
    )
    from yfin.storage.db import session_factory as session_factory_for

    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = SYMBOL_DATASETS.resolve(None if datasets.strip() == "all" else comma_list(datasets))

    start_date = parse_day(start, option="--start")
    end_date = parse_day(end, option="--end")
    if start_date is not None and end_date is not None and start_date > end_date:
        typer.echo("--start cannot be later than --end", err=True)
        raise typer.Exit(code=1)

    exchanges = comma_list(exchange, upper=True)
    quote_types = comma_list(quote_type, upper=True)
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
        codes = [nz.normalize_symbol(s) for s in comma_list(symbols)]
    else:
        stmt = select(Symbol.symbol).order_by(Symbol.symbol)
        if not include_inactive:
            stmt = stmt.where(Symbol.is_active.is_(True))
        with session_factory_for(engine)() as session:
            if filtered:
                codes = filtered_symbols(
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
            selector=selector(
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

    echo_tally(tally)
    raise typer.Exit(code=tally.exit_code())


@app.command("status")
def status(
    limit: Annotated[int, typer.Option("--limit")] = 1,
    scope: Annotated[str | None, typer.Option("--scope", help="symbols | market")] = None,
) -> None:
    """Summary of the most recent sync run."""
    from sqlalchemy import select

    from yfin.models import RunScope, SyncRun, SyncRunItem

    factory = session_factory()
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

    # Only with change publishing on. With the relay off by design a
    # permanently growing backlog is the expected state, and reporting it as
    # a number to worry about would be noise -- the same rule `yfin stream
    # status` applies to the tick relay.
    from yfin.core.config import get_settings

    if get_settings().yf_changes_enabled:
        from yfin.cli.changes import changes_lag_line

        typer.echo(changes_lag_line())


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
    from yfin.pipeline.prune import PruneDisabledError, run_prune

    settings = get_settings()
    factory = session_factory()
    try:
        with factory() as session:
            report = run_prune(
                session,
                enabled=settings.yf_prune_enabled or force,
                orphan_news=orphan_news,
                orphan_reports=orphan_reports,
                calendars_before=parse_date(calendars_before),
                history_before=parse_date(history_before),
                asof_before=parse_date(asof_before),
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


@app.command("datasets")
def list_datasets() -> None:
    """Lists available dataset names (all three registries)."""
    from yfin.datasets import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS

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

