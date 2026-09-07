"""Helpers the command modules share.

Split out when `cli/app.py` was broken up: these eight were private names
in that module, and four other command modules were already reaching into
it for them through a deferred import to dodge the circular dependency
(`from yfin.cli.app import _session_factory` inside a function body). A
shared module is what that import was asking for, so the names lost their
underscore -- they are the module's interface now, not its internals.

Nothing here imports the pipeline, the datasets or the models package, so
importing it costs nothing at startup.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import typer
from sqlalchemy import Engine, or_
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import get_settings
from yfin.core.logging_setup import configure_logging


def parse_date(value: str | None) -> datetime | None:
    """YYYY-MM-DD -> UTC-aware datetime.

    Columns are timestamptz. A naive bound would be interpreted by psycopg using
    the connection's timezone -- the value comes out correct but compares at a
    different awareness level than the column.
    """
    return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC) if value else None


def parse_day(value: str | None, *, option: str) -> date | None:
    """YYYY-MM-DD -> date. An invalid value never silently becomes None: a wrong
    range would otherwise look like it was applied."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        typer.echo(f"invalid date for {option}: {value} (expected YYYY-MM-DD)", err=True)
        raise typer.Exit(code=1) from None


def selector(
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


def normalize_filter_values(values: list[str]) -> list[str]:
    """Converts --exchange / --quote-type input to match the write path's casing.

    `datasets/symbols.py` writes these two columns with `.upper()`; if the
    filter did not match that casing, `--exchange nms` would silently return
    an empty result.
    """
    return [v.strip().upper() for v in values]


def filtered_symbols(
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
    from yfin.models import Symbol

    stmt = base_stmt
    if exchanges:
        stmt = stmt.where(Symbol.exchange.in_(normalize_filter_values(exchanges)))
    if quote_types:
        stmt = stmt.where(Symbol.quote_type.in_(normalize_filter_values(quote_types)))
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


def engine() -> Engine:
    # Imported here, not at module level: `yfin --help` reaches this module
    # for `parse_day` and `selector`, and `storage.db` pulls the model
    # package in behind it.
    from yfin.storage.db import create_db_engine

    settings = get_settings()
    # No `service` here. This is the shared engine helper behind two dozen
    # commands, and naming one of them would name the wrong one for the
    # rest; the entry points that ARE a service claim it themselves.
    configure_logging(settings.log_level, settings.log_format)
    return create_db_engine()


def session_factory() -> sessionmaker[Session]:
    from yfin.storage.db import session_factory as make

    return make(engine())


def echo_tally(tally: Any) -> None:
    """Run summary -- the same three lines `sync` and `market sync` print."""
    typer.echo(f"run #{tally.run_id}  symbols={tally.symbol_count}")
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.counts.items())))
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in tally.totals.items()))

