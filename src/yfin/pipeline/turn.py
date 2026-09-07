"""One turn of a non-symbol dataset: fetch, normalize, write, account.

The market and domain runners each had their own copy of this. The two
bodies differed only in log keys, the arity of `normalize`, and the shape
of the audit key -- everything load-bearing was identical: the error
boundary around fetch, the proxy-health accounting, the one-transaction
write, and the rollback that still leaves an audit row behind. That is
transaction and error policy, and it should not have two homes.

The symbol side does NOT use this. It runs many symbols across worker
threads with a retry loop around the transaction (`pipeline/persist.py`);
sharing a body with that would mean a parameter for every difference.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import sessionmaker

from yfin.core import metrics
from yfin.core.errors import classify_error
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import NormalizedResult
from yfin.datasets.meta import DatasetMeta
from yfin.datasets.registry import Registry
from yfin.pipeline.audit import ItemRecord, failed_records, record_items
from yfin.pipeline.contracts import ProxyTracker
from yfin.storage.changes import ChangeCollector, ChangeContext
from yfin.storage.contracts import RowWriter, WriteStats
from yfin.storage.persistence import PostgresRowWriter

log = get_logger(__name__)


@dataclass(frozen=True)
class Turn:
    """Everything that differs between one runner's turn and the other's."""

    dataset: DatasetMeta

    #: Fetch and normalize, already bound to their context. Two callables
    #: rather than a context object because the two runners disagree on
    #: `normalize`'s arity -- market passes the raw payload, domain also
    #: passes the domain key.
    fetch: Callable[[], Any]
    normalize: Callable[[Any], NormalizedResult]
    upsert: Callable[[RowWriter, NormalizedResult], WriteStats]

    #: Written to `sync_run_items.symbol`. Not a symbol on either of these
    #: runners: the market side writes the scope label, the domain side
    #: writes the domain's own symbol.
    audit_key: str

    #: Resolves `produces` to table names when a turn fails before any
    #: write, so a failure still leaves one row per table.
    registry: Registry[Any]

    #: Names the runner in the log line ("market" / "domain").
    kind: str

    #: Extra structured log fields; scope on one side, key and region on
    #: the other.
    log_context: Mapping[str, Any] = field(default_factory=dict)

    region: str | None = None

    #: How this turn's writes become change events, or None when
    #: `yf_changes_enabled` is off. The CONTEXT is on the turn because only
    #: the runner knows the run; the COLLECTOR is built per turn below, so a
    #: turn that rolls back publishes nothing.
    changes: ChangeContext | None = None


def run_turn(
    factory: sessionmaker[Any],
    turn: Turn,
    tracker: ProxyTracker | None = None,
) -> list[ItemRecord]:
    """Fetch, normalize and write one turn inside its own transaction."""
    started = time.perf_counter()

    def failed(exc: Exception) -> list[ItemRecord]:
        return failed_records(
            turn.audit_key,
            turn.dataset.name,
            f"{type(exc).__name__}: {exc}",
            turn.registry,
            region=turn.region,
        )

    try:
        result = turn.normalize(turn.fetch())
    except Exception as exc:  # noqa: BLE001 - this IS the error boundary
        kind = classify_error(exc)
        metrics.inc(
            "yfin_sync_yahoo_requests_total", dataset=turn.dataset.name, outcome="failed"
        )
        metrics.inc("yfin_sync_yahoo_errors_total", kind=kind.value)
        log.warning(
            f"{turn.kind} dataset failed",
            dataset=turn.dataset.name,
            kind=kind.value,
            error=str(exc),
            **turn.log_context,
        )
        if tracker is not None:
            tracker.record_error(kind, str(exc))
        return failed(exc)
    if tracker is not None:
        tracker.record_success()

    fetched = sum(len(w.rows) for w in result.writes)
    metrics.inc(
        "yfin_sync_yahoo_requests_total",
        dataset=turn.dataset.name,
        # `empty` is not a failure -- a market with no IPOs this week
        # legitimately returns nothing -- and the audit already keeps the
        # two apart. The counter has to as well, or a healthy quiet week
        # would look like an outage.
        outcome="ok" if fetched else "empty",
    )
    duration = int((time.perf_counter() - started) * 1000)

    with factory() as session:
        try:
            # Built here, not on the Turn: a turn whose write fails rolls
            # back, and its events must go with it rather than reach the
            # next turn's collector.
            collector = ChangeCollector(turn.changes) if turn.changes else None
            if collector is not None:
                collector.enter_dataset(turn.dataset.name)
            stats = turn.upsert(PostgresRowWriter(session, collector=collector), result)
            if collector is not None:
                # Last statement before the commit, so the window the relay
                # waits on stays as small as the transaction allows.
                collector.flush(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - the write boundary
            # Rollback first: the audit row is written by the caller from
            # what this returns, on a session that is not this one, so it
            # survives the rollback.
            session.rollback()
            log.error(
                f"{turn.kind} turn failed",
                dataset=turn.dataset.name,
                error=str(exc),
                **turn.log_context,
            )
            return failed(exc)

    return record_items(
        turn.dataset, turn.audit_key, stats, fetched, duration, region=turn.region
    )
