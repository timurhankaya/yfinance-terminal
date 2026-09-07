"""Scheduler commands: run, jobs.

`run` stays in the foreground and obeys SIGTERM. It does not daemonize --
the process is meant to run under systemd, Docker or Kubernetes, and all
three want a process that exits when told to. `cli/stream.py` says the same
about `stream run`, for the same reason.
"""

from __future__ import annotations

import typer

from yfin.cli.common import engine

scheduler_app = typer.Typer(help="Scheduled jobs (replaces cron)", no_args_is_help=True)


@scheduler_app.command("run")
def scheduler_run() -> None:
    """Runs scheduled jobs until stopped.

    Each firing spawns `yfin <command>` in its own process group. The
    scheduler replaces cron, not the runner: sharding, advisory locks and
    exit codes all stay where they are.

    Holds no advisory lock of its own. Two schedulers would double every
    job, but each job's own lock is what stops the damage -- and running two
    is an operator error that a lock here would only half-hide.
    """
    from sqlalchemy import Engine

    from yfin.core.config import get_settings
    from yfin.core.logging_setup import configure_logging
    from yfin.core.metrics import serve_metrics
    from yfin.scheduler.service import SchedulerService

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format, "scheduler")

    db = engine()
    assert isinstance(db, Engine)

    # The scheduler is the process that also hosts the database exporter, so
    # its port is the one Prometheus scrapes for everything read from a
    # table. 0 means off, which is the default.
    serve_metrics(settings.metrics_port)

    SchedulerService(db, settings=settings).run()


@scheduler_app.command("jobs")
def scheduler_jobs() -> None:
    """Lists every job, its cron, its queue and when it next fires.

    Every job is listed, including the disabled ones. A command that showed
    only what is scheduled would make "why did prune never run" a question
    with no visible answer.
    """
    from datetime import UTC, datetime

    from yfin.core.config import get_settings
    from yfin.scheduler.jobs import JOBS, interval_seconds

    settings = get_settings()
    typer.echo(f"{'job':<18} {'executor':<9} {'cron':<16} {'every':<10} next")

    for job in JOBS:
        cron = getattr(settings, job.setting, "").strip()
        if not cron:
            typer.echo(f"{job.name:<18} {job.executor:<9} {'(disabled)':<16} {'-':<10} -")
            continue
        try:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger.from_crontab(
                cron, timezone=settings.yf_schedule_timezone
            )
        except ImportError:
            typer.echo(
                f"{job.name:<18} {job.executor:<9} {cron:<16} {'?':<10} "
                'install the extra: pip install "yfin[scheduler]"'
            )
            continue

        now = datetime.now(UTC)
        nxt = trigger.get_next_fire_time(None, now)
        typer.echo(
            f"{job.name:<18} {job.executor:<9} {cron:<16} "
            f"{_humanise(interval_seconds(trigger, now=now)):<10} "
            f"{nxt.isoformat() if nxt else 'never'}"
        )

    typer.echo(f"\ntimezone: {settings.yf_schedule_timezone}")


def _humanise(seconds: float) -> str:
    """A cadence a reader can compare at a glance."""
    if seconds <= 0:
        return "-"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


__all__ = ["scheduler_app"]
