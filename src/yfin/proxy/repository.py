"""Reads and writes the proxies table.

Health updates happen in their own short transaction, outside the
symbol transaction: a rollback there would also erase which proxy got
banned.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from yfin.errors import ErrorKind
from yfin.logging_setup import get_logger, scrub
from yfin.models import Proxy, ProxyHealth
from yfin.proxy.health import (
    HealthEvent,
    ProxyHealthState,
    ProxyPolicy,
    apply_outcome,
    event_for,
)

log = get_logger(__name__)

def select_eligible(session: Session, limit: int) -> list[Proxy]:
    """Eligible proxies: healthy first, then never-tried, then fastest.

    This query is the single source of truth for eligibility. A Python
    helper mirroring the same rule used to exist unused in production and
    could drift from this one, so it was removed; the rule is tested
    directly against this SQL with a truth table.

    The `(health='unknown') DESC` ordering is required: without it, a
    newly added proxy (`last_latency_ms IS NULL`) would never be picked,
    while a proxy freshly out of cooldown with a stale measured latency
    would be preferred instead.
    """
    now = datetime.now(UTC)
    stmt = (
        select(Proxy)
        .where(
            Proxy.is_enabled.is_(True),
            Proxy.health != ProxyHealth.DEAD,
            (Proxy.cooldown_until.is_(None)) | (Proxy.cooldown_until <= now),
        )
        .order_by(
            case((Proxy.health == ProxyHealth.HEALTHY, 0), else_=1),
            case((Proxy.health == ProxyHealth.UNKNOWN, 0), else_=1),
            Proxy.last_latency_ms.is_(None),
            Proxy.last_latency_ms.asc(),
            Proxy.id.asc(),
        )
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def count_all(session: Session) -> int:
    return len(list(session.execute(select(Proxy.id)).scalars()))


def persist_event(
    session: Session,
    proxy_id: int,
    event: HealthEvent,
    *,
    policy: ProxyPolicy,
    now: datetime | None = None,
    error: str | None = None,
    latency_ms: int | None = None,
    checked: bool = False,
) -> ProxyHealthState:
    """Applies a single event to the DB. Must be called in its own transaction.

    Health updates never happen inside the symbol transaction: a rollback
    there would also erase which proxy got banned.

    Counters are written incrementally (`= x + 1`) because multiple
    writers can hit the same row (child's flush, parent's SHARD_CRASH,
    a concurrent `proxy check`); writing back a value read earlier would
    lose updates. The state transition itself runs under FOR UPDATE.
    """
    moment = now or datetime.now(UTC)
    row = session.execute(
        select(Proxy).where(Proxy.id == proxy_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return ProxyHealthState()

    before = ProxyHealthState.of(row)
    after = apply_outcome(before, event, moment, policy)

    values: dict[str, object] = {
        "health": after.health,
        "consecutive_failures": after.consecutive_failures,
        "cooldown_rounds": after.cooldown_rounds,
        "cooldown_until": after.cooldown_until,
    }
    if event is HealthEvent.SUCCESS:
        values["success_count"] = Proxy.success_count + 1
        values["last_ok_at"] = moment
    else:
        values["failure_count"] = Proxy.failure_count + 1
        values["last_error_at"] = moment
        values["last_error"] = scrub(f"{event.value}: {error}")[:1000] if error else event.value
    if latency_ms is not None:
        values["last_latency_ms"] = latency_ms
    if checked:
        values["last_checked_at"] = moment

    session.execute(update(Proxy).where(Proxy.id == proxy_id).values(**values))
    if before.health is not after.health:
        log.info(
            "proxy health changed",
            proxy=row.label,
            before=before.health.value,
            after=after.health.value,
            rounds=after.cooldown_rounds,
        )
    return after


class ShardProxyTracker:
    """Tracks one shard's proxy health in memory.

    Events accumulate in memory and are written to the DB (a) the moment
    the cooldown threshold is crossed, (b) at shard end. Once `withdrawn`
    is True the child stops pulling new symbols from the queue -
    otherwise a banned proxy gets 429s instantly and would drain the
    queue fastest, marking everything failed.
    """

    def __init__(self, proxy_id: int | None, policy: ProxyPolicy) -> None:
        self.proxy_id = proxy_id
        self.policy = policy
        self.state = ProxyHealthState()
        self._pending: list[tuple[HealthEvent, str | None]] = []
        self.withdrawn = False

    @property
    def active(self) -> bool:
        return self.proxy_id is not None

    def record_success(self) -> None:
        if not self.active:
            return
        self._append(HealthEvent.SUCCESS, None)

    def record_error(self, kind: ErrorKind, message: str | None = None) -> None:
        if not self.active:
            return
        event = event_for(kind)
        if event is None:
            return  # DATA / UNKNOWN_SYMBOL doesn't penalize the proxy
        self._append(event, message)

    def _append(self, event: HealthEvent, message: str | None) -> None:
        now = datetime.now(UTC)
        self._pending.append((event, message))
        self.state = apply_outcome(self.state, event, now, self.policy)
        if self.state.health in (ProxyHealth.COOLDOWN, ProxyHealth.DEAD):
            self.withdrawn = True

    def flush(self, session: Session) -> None:
        """Replays pending events against DB state, in order."""
        if not self.active or not self._pending:
            return
        pending, self._pending = self._pending, []
        assert self.proxy_id is not None
        for event, message in pending:
            persist_event(session, self.proxy_id, event, policy=self.policy, error=message)
        session.commit()


# --------------------------------------------------------------------------
# Active check
# --------------------------------------------------------------------------
