"""`yfin domain` -- sector and industry data."""

from __future__ import annotations

from typing import Annotated

import typer
from sqlalchemy import select

from yfin.cli.common import parse_day, session_factory
from yfin.core.config import get_settings
from yfin.core.logging_setup import configure_logging, get_logger
from yfin.core.text import comma_list

log = get_logger(__name__)

domain_app = typer.Typer(help="Sector / industry data", no_args_is_help=True)


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
    # Heavy imports live in the command body, not at module level: `yfin
    # --help` would otherwise build the whole dataset registry to print a
    # list of names.
    from yfin.datasets import DOMAIN_DATASETS
    from yfin.pipeline.audit import EXIT_LOCK_NOT_ACQUIRED
    from yfin.pipeline.domain_runner import RegionValidationError, run_domain_sync
    from yfin.storage.db import LockNotAcquired, create_db_engine

    settings = get_settings()
    if regions is not None:
        # The `--regions` override goes through a copy of Settings:
        # `domain_regions()` only reads `settings.yf_domain_regions`, so
        # validation (format + empirical probe) applies to the CLI value the
        # same way. Same pattern as `--shards` overriding YF_MAX_SHARDS; the
        # difference is that value has nothing to validate.
        settings = settings.model_copy(update={"yf_domain_regions": regions})
    configure_logging(settings.log_level, settings.log_format)
    engine = create_db_engine(settings)

    selected = DOMAIN_DATASETS.resolve(None if datasets.strip() == "all" else comma_list(datasets))

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
    from yfin.pipeline.domain_audit import audit_domains

    factory = session_factory()
    day = parse_day(as_of, option="--as-of")
    with factory() as session:
        report = audit_domains(session, as_of=day, run_id=run)

    typer.echo(f"sectors   : {report.sector_count}")
    typer.echo(
        f"industries: {report.industry_count} "
        f"(the API reports: {report.expected_industries})"
    )
    if report.cells_by_status:
        typer.echo("cells     : " + "  ".join(
            f"{k}={v}" for k, v in sorted(report.cells_by_status.items())
        ))
    for problem in report.problems:
        typer.echo(f"PROBLEM: {problem}", err=True)
    if not report.ok:
        raise typer.Exit(code=report.exit_code())
    typer.echo("audit ok")


@domain_app.command("datasets")
def domain_datasets() -> None:
    """Lists domain dataset names."""
    from yfin.datasets import DOMAIN_DATASETS

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
    from yfin.models import Domain, DomainType

    factory = session_factory()
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

