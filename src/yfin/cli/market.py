"""`yfin market` and `yfin screen` -- market-scoped datasets.

Both groups run through `market_runner` and take the `yfin_market_sync`
lock; the screener is a market dataset with a variant axis, not a separate
kind of thing.
"""

from __future__ import annotations

from typing import Annotated

import typer

from yfin.cli.common import echo_tally, parse_day, session_factory
from yfin.core.config import get_settings
from yfin.core.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

market_app = typer.Typer(help="Market (Market/Calendars) data", no_args_is_help=True)
screen_app = typer.Typer(help="Screener screens", no_args_is_help=True)


@screen_app.command("sync")
def screen_sync(
    screens: Annotated[str | None, typer.Option("--screens")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages")] = None,
) -> None:
    """Fetches screens.

    Takes the `yfin_market_sync` lock; can run concurrently with symbol sync.
    """
    # Heavy imports live in the command body, not at module level: `yfin
    # --help` would otherwise build the whole dataset registry to print a
    # list of names.
    from yfin.datasets import MARKET_DATASETS
    from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED
    from yfin.pipeline.market_runner import run_market_sync
    from yfin.storage.db import LockNotAcquired, create_db_engine

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
    echo_tally(summary)
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
    from sqlalchemy import text

    factory = session_factory()
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
    # Heavy imports live in the command body, not at module level: `yfin
    # --help` would otherwise build the whole dataset registry to print a
    # list of names.
    from yfin.datasets import MARKET_DATASETS
    from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED
    from yfin.pipeline.market_runner import run_market_sync
    from yfin.storage.db import LockNotAcquired, create_db_engine

    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)

    selected = MARKET_DATASETS.resolve(None if datasets.strip() == "all" else datasets.split(","))
    # `_parse_day` gives a meaningful message + exit 1 on an invalid date. A raw
    # strptime ValueError would fall through to main()'s generic handler and
    # show the user "unexpected error" -- `sync` already did this correctly.
    window_start = parse_day(start, option="--start")
    window_end = parse_day(end, option="--end")

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
    from yfin.datasets import MARKET_DATASETS

    typer.echo(", ".join(MARKET_DATASETS.user_visible_names()))

