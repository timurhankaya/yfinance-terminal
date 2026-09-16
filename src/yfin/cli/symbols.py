"""`yfin symbols` and `yfin discover` -- the symbol universe.

Two ends of one workflow: `discover term` finds candidates and writes them
inactive, `symbols activate` promotes them."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from yfin.cli.common import (
    echo_tally,
    session_factory,
)
from yfin.core.logging_setup import get_logger
from yfin.core.text import comma_list

log = get_logger(__name__)

symbols_app = typer.Typer(help="Symbol universe management", no_args_is_help=True)
discover_app = typer.Typer(help="Free-text term discovery", no_args_is_help=True)


@symbols_app.command("add")
def symbols_add(
    symbols: Annotated[list[str], typer.Argument(help="Symbol codes")],
) -> None:
    """Adds symbols. strip().upper() is applied."""
    # Imported in the body, not at module level: `cli/app.py` imports this
    # module to mount the group, so a module-level import would put the
    # model package and numpy behind `yfin --help`.
    from yfin.core import normalize as nz
    from yfin.models import Symbol

    factory = session_factory()
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
    from sqlalchemy import select

    from yfin.models import Symbol

    factory = session_factory()
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
    from sqlalchemy import func, select

    from yfin.models import Symbol

    factory = session_factory()
    stmt = (
        select(
            Symbol.exchange,
            Symbol.full_exchange_name,
            Symbol.quote_type,
            func.count().label("count"),
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
        code = exchange or "NULL (unresolved)"
        typer.echo(f"{code:<20} {quote_type or '-':<16} {full_name or '':<32} {count}")


@symbols_app.command("deactivate")
def symbols_deactivate(symbol: str) -> None:
    """Soft delete: is_active=0. No data is deleted."""
    from sqlalchemy import update

    from yfin.core import normalize as nz
    from yfin.models import Symbol

    code = nz.normalize_symbol(symbol)
    factory = session_factory()
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
    ON DELETE RESTRICT; the deletes live in `storage/purge.py` so the change
    collector sees them."""
    from yfin.core import normalize as nz
    from yfin.storage.purge import purge_symbol

    code = nz.normalize_symbol(symbol)
    if not force:
        typer.echo("this command is irreversible; pass --force to confirm")
        raise typer.Exit(code=1)

    factory = session_factory()
    with factory() as session:
        removed = purge_symbol(session, code)
        session.commit()
    total = sum(removed.values())
    typer.echo(f"deleted: {code} ({total} row(s) across {len(removed)} table(s))")


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

    Preview with `--dry-run`: a screen can report thousands of symbols, and
    activating them multiplies `yfin sync`'s daily request count."""
    from sqlalchemy import select, update

    from yfin.models import Symbol

    conditions: list[Any] = [Symbol.is_active.is_(False)]
    if discovered_by:
        conditions.append(Symbol.discovered_by == discovered_by)
    if exchange:
        conditions.append(Symbol.exchange == exchange.upper())
    if quote_type:
        conditions.append(Symbol.quote_type == quote_type.upper())

    factory = session_factory()
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


@discover_app.command("term")
def discover_term(
    query: str,
    datasets: Annotated[str, typer.Option("--datasets")] = "search,lookup",
) -> None:
    """Runs Search/Lookup with a free-text term.

    Does not loop over symbols: the shard machinery would be pointless for a
    handful of terms, so it runs as a single process at `market_runner` scale.
    """
    from yfin.core.config import get_settings
    from yfin.core.logging_setup import configure_logging
    from yfin.datasets import SYMBOL_DATASETS
    from yfin.models.discovery import QUERY_TERM_LENGTH
    from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED
    from yfin.pipeline.runner import run_sync
    from yfin.storage.db import LockNotAcquired, create_db_engine

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

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

    selected = SYMBOL_DATASETS.resolve(comma_list(datasets))
    engine = create_db_engine(settings)
    try:
        summary = run_sync(engine, [term], selected, settings=settings, selector=f"term={term}")
    except LockNotAcquired:
        typer.echo("another sync is running (advisory lock not acquired)", err=True)
        raise typer.Exit(code=EXIT_LOCK_NOT_ACQUIRED) from None
    echo_tally(summary)
    raise typer.Exit(code=summary.exit_code())

