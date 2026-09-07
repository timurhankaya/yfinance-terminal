"""Live stream commands: run, status, scope.

Kept out of `cli/app.py` for the same reason `cli/bars.py` is -- these
share no state with the sync commands. No SQL lives here; the reads go
through `stream/repository.py` and the process wiring through
`stream/runner.py`.

`run` stays in the foreground and obeys SIGTERM. It does not daemonize:
the process is meant to run under systemd, Docker or Kubernetes, and
those all want a process that exits when told to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

import typer
from sqlalchemy import func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.models import Symbol
from yfin.models.stream import StreamScope
from yfin.stream.runner import StreamDisabled, canary_symbols, run_stream
from yfin.stream.topology import QuotaExceeded, plan_connections

stream_app = typer.Typer(help="Live WebSocket tick stream", no_args_is_help=True)
stream_scope_app = typer.Typer(help="Streaming scope (stream_scope)", no_args_is_help=True)
stream_app.add_typer(stream_scope_app, name="scope")


def _factory() -> sessionmaker[Session]:
    # Lazy import: cli/app.py imports this module.
    from yfin.cli.app import _session_factory

    return _session_factory()


def _engine() -> object:
    from yfin.cli.app import _engine as engine_factory

    return engine_factory()


# --- run --------------------------------------------------------------------


@stream_app.command("run")
def stream_run() -> None:
    """Run the stream in the foreground until SIGTERM.

    Holds the `yfin_stream` advisory lock, so a second copy refuses to
    start rather than writing the same ticks twice. The lock is separate
    from `yfin_sync`: the two write different tables and may run together.
    """
    from sqlalchemy import Engine

    engine = _engine()
    assert isinstance(engine, Engine)
    settings = get_settings()
    try:
        result = run_stream(engine, settings)
    except StreamDisabled as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"rows written: {result.rows_written:,}")
    if result.exit_code:
        # A dead writer means the process was up while collecting nothing;
        # the exit code has to say so or a supervisor would restart it
        # only by luck.
        typer.echo("writer thread failed; see stream_sessions for the session status")
    raise typer.Exit(code=result.exit_code)


# --- status -----------------------------------------------------------------


@stream_app.command("status")
def stream_status() -> None:
    """Connection health, subscription sizes and staleness.

    The `stale` column is the one to read first. Connection rows keep
    whatever state they had when the process stopped, so a killed process
    leaves them saying `open` -- only the heartbeat age reveals that
    nothing is running.
    """
    from sqlalchemy import Engine

    from yfin.stream.runner import build_repository

    engine = _engine()
    assert isinstance(engine, Engine)
    settings = get_settings()
    repository = build_repository(engine)

    rows = repository.health_rows(
        stale_after_seconds=settings.yf_stream_rescan_seconds * 2
    )
    if not rows:
        typer.echo("no connections recorded")
    else:
        typer.echo(
            f"{'connection':<16} {'state':<13} {'symbols':>7} {'reconn':>6} "
            f"{'last msg':<20} {'canary':<20} stale"
        )
        for row in rows:
            typer.echo(
                f"{row['connection_key']!s:<16} {row['state']!s:<13} "
                f"{row['subscribed_count']!s:>7} {row['reconnect_count']!s:>6} "
                f"{_ago(row['last_message_at']):<20} {_ago(row['last_canary_at']):<20} "
                f"{'YES' if row['stale'] else '-'}"
            )
        silent = [r for r in rows if r["last_canary_at"] is None and r["state"] == "open"]
        if silent:
            # The server never reports a truncated subscription, so a
            # silent canary is the only evidence that one happened.
            typer.echo(
                f"\n{len(silent)} open connection(s) have never seen the canary: "
                f"the subscription may be truncated"
            )

    with _factory()() as session:
        scoped = session.execute(
            select(func.count()).select_from(StreamScope).where(StreamScope.enabled)
        ).scalar_one()
        pending = session.execute(
            text(
                "SELECT count(*) FROM stream_sessions WHERE status = 'running'"
            )
        ).scalar_one()
    typer.echo(f"\nscope: {scoped} symbol(s) enabled; running sessions: {pending}")


def _ago(value: object) -> str:
    if not isinstance(value, datetime):
        return "never"
    delta = datetime.now(UTC) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


# --- scope ------------------------------------------------------------------


@stream_scope_app.command("add")
def scope_add(
    symbols: Annotated[list[str], typer.Argument(help="Symbol codes")],
    no_archive: Annotated[
        bool, typer.Option("--no-archive", help="Quote the symbol but do not keep its ticks")
    ] = False,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Add symbols to the streaming scope.

    Unlike `bars scope`, this table is a plain set: a missing row always
    means out of scope, so `disable` may delete rather than having to keep
    a row around.
    """
    settings = get_settings()
    archive = settings.yf_stream_archive_default and not no_archive
    codes = [nz.normalize_symbol(code) for code in symbols]

    with _factory()() as session:
        known = set(
            session.execute(
                select(Symbol.symbol).where(Symbol.symbol.in_(codes))
            ).scalars()
        )
        unknown = [code for code in codes if code not in known]
        if unknown:
            typer.echo(f"not in the symbol universe: {', '.join(unknown)}")
            raise typer.Exit(code=1)

        for code in codes:
            session.execute(
                text(
                    "INSERT INTO stream_scope (symbol, enabled, archive, added_at, note) "
                    "VALUES (:s, true, :a, :ts, :n) "
                    "ON CONFLICT (symbol) DO UPDATE SET enabled = true, archive = :a, note = :n"
                ),
                {"s": code, "a": archive, "ts": datetime.now(UTC), "n": note},
            )

        # Same warning the sync CLI gives for filters: `yfin symbols add`
        # writes only the symbol, so exchange stays NULL until the first
        # sync -- and every such symbol lands on one `unknown` connection.
        missing = session.execute(
            select(func.count())
            .select_from(Symbol)
            .where(Symbol.symbol.in_(codes), Symbol.exchange.is_(None))
        ).scalar_one()
        session.commit()

    typer.echo(f"added {len(codes)} symbol(s); archive={archive}")
    if missing:
        typer.echo(
            f"warning: {missing} of them have no exchange yet and will share the "
            f"'unknown' connection until the next sync"
        )


@stream_scope_app.command("disable")
def scope_disable(
    symbols: Annotated[list[str], typer.Argument(help="Symbol codes")],
) -> None:
    """Remove symbols from the streaming scope and drop their quotes."""
    from sqlalchemy import Engine

    from yfin.stream.runner import build_repository

    codes = [nz.normalize_symbol(code) for code in symbols]
    with _factory()() as session:
        result = session.execute(
            text("DELETE FROM stream_scope WHERE symbol = ANY(:s)"), {"s": codes}
        )
        session.commit()
        # CursorResult, not Result: rowcount lives on the DML form only.
        removed = cast("CursorResult[Any]", result).rowcount or 0

    engine = _engine()
    assert isinstance(engine, Engine)
    # live_quotes does not trim itself: left alone it only grows and
    # `stream status` reports prices for symbols nobody streams.
    quotes = build_repository(engine).delete_quotes(codes)
    typer.echo(f"removed {removed} scope row(s), {quotes} quote row(s)")


@stream_scope_app.command("list")
def scope_list(
    exchange: Annotated[
        str | None, typer.Option("--exchange", help="Filter by exchange code")
    ] = None,
) -> None:
    """List the streaming scope, with the connection each symbol lands on."""
    settings = get_settings()
    query = (
        select(StreamScope.symbol, StreamScope.enabled, StreamScope.archive, Symbol.exchange)
        .join(Symbol, Symbol.symbol == StreamScope.symbol)
        .order_by(StreamScope.symbol)
    )
    if exchange is not None:
        # Normalised on the INPUT, never by wrapping the column: a
        # func.upper() here would make ix_symbols_exchange unusable.
        query = query.where(Symbol.exchange == exchange.strip().upper())

    with _factory()() as session:
        rows = session.execute(query).all()

    if not rows:
        typer.echo("scope is empty")
        return

    try:
        plans = plan_connections(
            [(row[0], row[3]) for row in rows if row[1]],
            max_symbols_per_connection=settings.yf_stream_max_symbols_per_connection,
            max_connections=settings.yf_stream_max_connections,
            canary_count=len(canary_symbols(settings)),
        )
    except QuotaExceeded as exc:
        typer.echo(f"scope does not fit the current limits: {exc}")
        raise typer.Exit(code=1) from exc

    placement = {
        symbol: plan.key for plan in plans for symbol in plan.symbols
    }
    typer.echo(f"{'symbol':<16} {'exchange':<10} {'archive':<8} connection")
    for symbol, enabled, archive, exch in rows:
        typer.echo(
            f"{symbol:<16} {exch or '-':<10} {'yes' if archive else 'no':<8} "
            f"{placement.get(symbol, 'disabled' if not enabled else '-')}"
        )
    typer.echo(f"\n{len(rows)} symbol(s) across {len(plans)} connection(s)")


# --- relay ------------------------------------------------------------------


@stream_app.command("relay")
def stream_relay(
    once: Annotated[
        bool, typer.Option("--once", help="Publish one batch and exit")
    ] = False,
) -> None:
    """Publish outbox rows to Kafka.

    A separate process from `stream run` on purpose: a broker outage must
    not slow down or stop collection. The outbox is written inside the
    tick transaction, so a row is queued if and only if it is archived.

    Holds `yfin_stream_relay`. Two relays would read the same offset,
    publish the same messages and could roll each other's progress back --
    the single-row constraint on the offset table does not prevent that,
    only this lock does.
    """
    from sqlalchemy import Engine

    from yfin.storage.db import advisory_lock
    from yfin.stream.relay import RELAY_LOCK_NAME, OutboxRelay, RelayConfig
    from yfin.stream.runner import session_factory_for

    settings = get_settings()
    if not settings.yf_kafka_enabled:
        typer.echo(
            "yf_kafka_enabled is off; enable it with "
            "`yfin config set yf_kafka_enabled true`"
        )
        raise typer.Exit(code=1)

    engine = _engine()
    assert isinstance(engine, Engine)
    factory = session_factory_for(engine)
    config = RelayConfig(
        bootstrap_servers=settings.yf_kafka_bootstrap_servers,
        topic_pattern=settings.yf_kafka_topic_pattern,
        batch_size=settings.yf_kafka_relay_batch,
    )
    relay = OutboxRelay(factory, config)

    with advisory_lock(engine, RELAY_LOCK_NAME):
        missing = relay.verify_topics()
        if missing:
            # Not fatal, but worth saying out loud: a topic created
            # implicitly takes the broker's default partition count, and
            # the wrong count silently costs per-symbol ordering.
            typer.echo(f"topics not present on the broker: {', '.join(missing)}")

        if once:
            from yfin.stream.kafka import build_producer

            published = relay.publish_once(build_producer(config.bootstrap_servers))
            typer.echo(f"published {published} message(s)")
        else:
            stats = relay.run()
            typer.echo(
                f"published {stats.published} message(s) over {stats.passes} pass(es)"
            )
    if relay.stats.last_error:
        typer.echo(f"last error: {relay.stats.last_error}")
        raise typer.Exit(code=1)


# --- reconcile --------------------------------------------------------------


@stream_app.command("reconcile")
def stream_reconcile(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Gaps per pass")] = None,
) -> None:
    """Fill open 1m bar gaps from the tick archive.

    Takes the `yfin_sync` lock, not the stream's: this writes price_bars
    and bar_gaps, which is exactly what a scheduled sync writes. The
    stream process and a sync can run together precisely because they do
    not share tables -- this command does, so it queues behind sync.

    `retention_expired` gaps are included, and they are the reason this
    exists: Yahoo drops 1m data after 29 days, so those windows can never
    be fetched again and the tick archive is the only thing left that
    knows what happened in them.
    """
    from sqlalchemy import Engine

    from yfin.storage.db import SYNC_LOCK_NAME, advisory_lock
    from yfin.stream.reconcile import reconcile_gaps
    from yfin.stream.runner import session_factory_for

    engine = _engine()
    assert isinstance(engine, Engine)
    factory = session_factory_for(engine)

    with advisory_lock(engine, SYNC_LOCK_NAME):
        stats = reconcile_gaps(factory, dry_run=dry_run, limit=limit)

    typer.echo(stats.summary)
    if stats.minutes_skipped_existing:
        typer.echo(
            f"{stats.minutes_skipped_existing} minute(s) already had a bar and were left alone"
        )
    if stats.gaps_without_timezone:
        # Refused rather than guessed: local_date is the exchange's
        # calendar day, and deriving it from UTC lands a day early for
        # positive-offset exchanges.
        unique = sorted(set(stats.gaps_without_timezone))
        typer.echo(
            f"{len(unique)} symbol(s) skipped for having no timezone: "
            f"{', '.join(unique[:10])}"
        )
    if dry_run:
        typer.echo("(dry run: nothing was written)")
