"""Pipeline change-event commands: relay, status.

The second relay, and deliberately a second PROCESS. Two outboxes, two
advisory locks, two offsets, two `client.id`s: a broker outage on the tick
side must not stall the pipeline side, and the two queues are walked
differently -- ticks by row id, changes by transaction id. See
`outbox/cursor.py` for why that difference is not a preference.

Its own module rather than a group inside `cli/stream.py` for the reason
`cli/bars.py` is separate: these commands share no state with the stream,
and the stream's docstring is about a socket that has to stay connected.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated

import typer

from yfin.cli.common import engine, session_factory
from yfin.outbox.spec import CHANGES_OUTBOX, OutboxSpec

changes_app = typer.Typer(
    help="Pipeline change events (pipeline_outbox)", no_args_is_help=True
)


def _disabled() -> None:
    typer.echo(
        "yf_changes_enabled is off; enable it with "
        "`yfin config set yf_changes_enabled true`"
    )
    raise typer.Exit(code=1)


def _spec(topic_pattern: str) -> OutboxSpec:
    """`outbox/spec.py` is a leaf -- dataclasses and typing, nothing else --
    so importing it at module level costs `yfin --help` nothing.

    The pattern is an operator setting, so it overrides the spec's own
    default rather than sitting beside it as a second source of truth.
    """
    return replace(CHANGES_OUTBOX, topic_pattern=topic_pattern)


@changes_app.command("relay")
def changes_relay(
    once: Annotated[
        bool, typer.Option("--once", help="Publish one batch and exit")
    ] = False,
) -> None:
    """Publish pipeline change events to Kafka.

    Holds `yfin_pipeline_relay`, which is NOT the stream relay's lock: the
    two relays run at the same time and must not serialise on each other.

    The contract is at-least-once. The offset moves only after every
    delivery is acknowledged, so a broker outage retries the batch rather
    than stepping past it -- and the outbox is the only place those rows
    exist. Consumers dedupe on the `yfin-outbox-id` header.
    """
    from sqlalchemy import Engine

    from yfin.core.config import get_settings
    from yfin.outbox.relay import OutboxRelay, RelayConfig
    from yfin.storage.db import advisory_lock

    settings = get_settings()
    if not settings.yf_changes_enabled:
        _disabled()

    db = engine()
    assert isinstance(db, Engine)
    factory = session_factory()
    config = RelayConfig(
        bootstrap_servers=settings.yf_kafka_bootstrap_servers,
        batch_size=settings.yf_kafka_relay_batch,
    )
    spec = _spec(settings.yf_changes_topic_pattern)
    relay = OutboxRelay(factory, config, spec)

    with advisory_lock(db, spec.lock_name):
        missing = relay.verify_topics()
        if missing:
            # Verified, never created: the partition count is the operator's
            # decision, an implicitly created topic takes the broker default,
            # and raising it later reorders keys.
            typer.echo(f"topics not present on the broker: {', '.join(missing)}")

        if once:
            from yfin.outbox.kafka import build_producer

            published = relay.publish_once(
                build_producer(config.bootstrap_servers, client_id=spec.client_id)
            )
            typer.echo(f"published {published} message(s)")
        else:
            stats = relay.run()
            typer.echo(
                f"published {stats.published} message(s) over {stats.passes} pass(es)"
            )
    if relay.stats.last_error:
        typer.echo(f"last error: {relay.stats.last_error}")
        raise typer.Exit(code=1)


@changes_app.command("status")
def changes_status() -> None:
    """How far behind the change relay is, and whether it is blocked.

    Three numbers rather than two, because a growing backlog has two
    unrelated causes and one fix each. The relay cannot pass an OPEN WRITING
    transaction anywhere in the database -- a long bar backfill, a `psql`
    session left idle in transaction -- so when the third number is set the
    relay is not behind, it is waiting, and the thing to look at is
    `pg_stat_activity` rather than the broker.
    """
    from yfin.core.config import get_settings

    settings = get_settings()
    if not settings.yf_changes_enabled:
        _disabled()
    typer.echo(changes_lag_line())


def changes_lag_line() -> str:
    """The one line `yfin status` and `yfin changes status` both print."""
    from yfin.outbox.relay import relay_lag

    lag = relay_lag(session_factory(), CHANGES_OUTBOX)
    line = (
        f"changes: {lag.rows} row(s) unpublished; "
        f"oldest {lag.oldest_age_seconds}s behind"
    )
    if lag.held_back_seconds is not None:
        line += f"; held back by an open transaction for {lag.held_back_seconds}s"
    return line


__all__ = ["changes_app", "changes_lag_line"]
