"""price_bars CLI commands: scope, gaps, maintenance, rescaling.

Kept SEPARATE from `cli.py`: that file was 630 lines and none of these
commands share state with the existing ones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from yfin.cli.common import session_factory
from yfin.core import normalize as nz
from yfin.models import BAR_INTERVALS, BarGap, IntradayScope
from yfin.storage.rescale import apply_pending, seed_baseline, unseeded_historic_splits

scope_app = typer.Typer(help="Intraday scope management (intraday_scope)", no_args_is_help=True)
bars_app = typer.Typer(help="price_bars maintenance and audit", no_args_is_help=True)


def _factory() -> sessionmaker[Session]:
    return session_factory()


# --- scope ------------------------------------------------------------------


@scope_app.command("add")
def scope_add(
    symbols: Annotated[list[str], typer.Argument(help="Symbol codes")],
    interval: Annotated[str, typer.Option("--interval", help="Bar interval")] = "1m",
    note: Annotated[str | None, typer.Option("--note")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Confirm the FIRST row for a non-1m interval")
    ] = False,
) -> None:
    """Add symbols to intraday_scope.

    Adding the FIRST row for an interval other than 1m is DANGEROUS:
    resolution checks "is there at least one row for this interval", so a
    single row excludes every OTHER symbol from that scope.
    """
    if interval not in BAR_INTERVALS:
        typer.echo(f"unknown interval: {interval}; valid: {', '.join(BAR_INTERVALS)}")
        raise typer.Exit(code=1)

    codes = [nz.normalize_symbol(s) for s in symbols]
    with _factory()() as session:
        already = session.execute(
            select(func.count())
            .select_from(IntradayScope)
            .where(IntradayScope.bar_interval == interval)
        ).scalar_one()
        if interval != "1m" and not already and not force:
            typer.echo(
                f"'{interval}' currently runs for the WHOLE universe. Adding the first "
                "row drops every other symbol out of scope; pass --force to confirm."
            )
            raise typer.Exit(code=1)

        now = datetime.now(UTC)
        for code in codes:
            session.merge(
                IntradayScope(
                    symbol=code, bar_interval=interval, enabled=True, added_at=now, note=note
                )
            )
        session.commit()
    typer.echo(f"{len(codes)} symbols added to the {interval} scope")


@scope_app.command("disable")
def scope_disable(
    symbols: Annotated[list[str], typer.Argument()],
    interval: Annotated[str, typer.Option("--interval")] = "1m",
) -> None:
    """Remove from scope but DOES NOT DELETE THE ROW (enabled=0).

    Deleting the last row for an interval would silently reopen scope to the
    entire universe; disable carries no such risk.
    """
    codes = [nz.normalize_symbol(s) for s in symbols]
    with _factory()() as session:
        session.execute(
            update(IntradayScope)
            .where(IntradayScope.symbol.in_(codes), IntradayScope.bar_interval == interval)
            .values(enabled=False)
        )
        session.commit()
    typer.echo(f"{len(codes)} symbols removed from the {interval} scope (enabled=0)")


@scope_app.command("list")
def scope_list(interval: Annotated[str | None, typer.Option("--interval")] = None) -> None:
    """Show the scope table and each interval's RESOLVED meaning."""
    with _factory()() as session:
        stmt = select(
            IntradayScope.bar_interval, IntradayScope.symbol, IntradayScope.enabled
        ).order_by(IntradayScope.bar_interval, IntradayScope.symbol)
        if interval:
            stmt = stmt.where(IntradayScope.bar_interval == interval)
        rows = list(session.execute(stmt))

    grouped: dict[str, list[tuple[str, bool]]] = {}
    for bar_interval, symbol, enabled in rows:
        grouped.setdefault(bar_interval, []).append((symbol, enabled))

    for name in BAR_INTERVALS:
        if interval and name != interval:
            continue
        entries = grouped.get(name, [])
        if not entries:
            meaning = "NO symbols" if name == "1m" else "WHOLE universe"
            typer.echo(f"{name:5s} no rows -> {meaning}")
            continue
        active = sum(1 for _s, e in entries if e)
        typer.echo(f"{name:5s} {active} active / {len(entries)} rows")
        for symbol, enabled in entries:
            typer.echo(f"      {'+' if enabled else '-'} {symbol}")


# --- gaps and maintenance -----------------------------------------------


@bars_app.command("gaps")
def bars_gaps(
    symbol: Annotated[str | None, typer.Option("--symbol")] = None,
    interval: Annotated[str | None, typer.Option("--interval")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    open_only: Annotated[bool, typer.Option("--open-only")] = False,
) -> None:
    """Missed windows."""
    with _factory()() as session:
        stmt = select(
            BarGap.symbol,
            BarGap.bar_interval,
            BarGap.gap_start_utc,
            BarGap.gap_end_utc,
            BarGap.reason,
            BarGap.resolved_at,
        ).order_by(BarGap.detected_at.desc())
        if symbol:
            stmt = stmt.where(BarGap.symbol == nz.normalize_symbol(symbol))
        if interval:
            stmt = stmt.where(BarGap.bar_interval == interval)
        if reason:
            stmt = stmt.where(BarGap.reason == reason)
        if open_only:
            stmt = stmt.where(BarGap.resolved_at.is_(None))
        rows = list(session.execute(stmt))

    if not rows:
        typer.echo("no gaps")
        return
    for sym, iv, start, end, why, resolved in rows:
        state = "resolved" if resolved else "OPEN"
        typer.echo(f"{sym:12s} {iv:4s} {start} -> {end}  {why:18s} {state}")


@bars_app.command("maintain")
def bars_maintain(dry_run: Annotated[bool, typer.Option("--dry-run")] = False) -> None:
    """Monthly maintenance. NEVER DELETES anything.

    Two steps were REMOVED, both as a direct result of the engine switch:

      * PARTITION ADVANCE. price_bars is now a TimescaleDB hypertable and
        creates its own chunks AT WRITE TIME. There is no "out-of-range
        insert" case, so skipping this maintenance has no cost either. The
        entire `maintenance.py` module was deleted.

      * ORPHAN-ROW AUDIT. The FK had been sacrificed for partitioning
        (MySQL ERROR 1506); since a hypertable can be the referencing side,
        price_bars now CARRIES an FK to symbols and integrity is guaranteed
        at the DB level. The query became dead code.
    """
    with _factory()() as session:
        # 1) Baseline-seed audit
        unseeded = unseeded_historic_splits(session)
        if unseeded:
            typer.echo(
                f"WARNING: {unseeded} historical splits are unseeded. Running "
                "`yfin sync --datasets bars` before `yfin bars rescale --seed` "
                "CORRUPTS THE ARCHIVE."
            )

        # 2) Gap summary
        for why, total, still_open in session.execute(
            text(
                # MySQL implicitly cast `SUM(x IS NULL)`'s boolean to int.
                # PostgreSQL has no SUM(boolean) (42883); FILTER is both
                # correct and more readable.
                "SELECT reason, COUNT(*), "
                "       COUNT(*) FILTER (WHERE resolved_at IS NULL) "
                "  FROM bar_gaps GROUP BY reason"
            )
        ).all():
            typer.echo(f"gap {why:18s} total {total}, open {still_open}")

        if dry_run:
            session.rollback()
        else:
            session.commit()


@bars_app.command("rescale")
def bars_rescale(
    seed: Annotated[
        bool, typer.Option("--seed", help="Seed a baseline record for existing splits")
    ] = False,
    symbol: Annotated[str | None, typer.Option("--symbol")] = None,
) -> None:
    """Retroactive rescaling.

    `--seed` must be run ONCE AT SETUP, BEFORE `bars_*` runs for the first
    time. Skipping it makes the first run apply every historical split in
    the splits table and re-split bars that Yahoo already returned at the
    current scale.
    """
    with _factory()() as session:
        if seed:
            count = seed_baseline(session)
            session.commit()
            typer.echo(f"baseline: {count} splits seeded")
            return
        if symbol is None:
            typer.echo("pass --seed or --symbol")
            raise typer.Exit(code=1)
        applied = apply_pending(session, nz.normalize_symbol(symbol))
        session.commit()
        typer.echo(f"{applied} split uygulandi")
