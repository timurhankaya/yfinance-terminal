"""Which proxies a run gets, and how a run reports one that died.

Eligibility (SQL, decides the shard count) and endpoint building (needs
`yf_proxy_secret_key`, can fail per row) stay separate functions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import Settings
from yfin.core.logging_setup import get_logger
from yfin.models import Proxy
from yfin.proxy import (
    HealthEvent,
    PasswordUndecryptable,
    ProxyPolicy,
    ShardProxyTracker,
    count_all,
    endpoint_of,
    persist_event,
    select_eligible,
)

log = get_logger(__name__)


class NoEligibleProxy(RuntimeError):
    """--require-proxy was given but no eligible proxy exists."""


@dataclass(frozen=True)
class ProxyPlan:
    """One shard's proxy assignment. Parent-side only.

    Never crosses the process boundary: `shard.py` flattens it into
    `ShardSpec`'s scalar fields before pickling.
    """

    proxy_id: int
    proxy_label: str
    dsn: str | None


def eligible_proxies(
    session: Session,
    *,
    settings: Settings,
    max_shards: int | None,
    no_proxy: bool,
    require_proxy: bool,
) -> list[Proxy]:
    """Eligible proxies; an empty list means a single, direct connection.

    shard_count = 1 with --no-proxy, else max(1, min(N, |eligible|)). A
    shard is never opened without a proxy except that single direct one.
    """
    if no_proxy:
        if require_proxy:
            # The two together are meaningless, and silently letting
            # --no-proxy win would take on ban risk without telling the user.
            raise ValueError("--no-proxy and --require-proxy cannot be given together")
        if max_shards is not None and max_shards > 1:
            # Running N shards from the same egress IP multiplies effective
            # rate by N; the rate limit is defined per shard.
            log.warning("--no-proxy reduced the shard count to 1", requested=max_shards)
        return []

    limit = max_shards if max_shards is not None else settings.yf_max_shards
    eligible = select_eligible(session, max(1, limit))
    if eligible:
        return eligible

    total = count_all(session)
    if require_proxy:
        raise NoEligibleProxy(
            f"no eligible proxy ({total} rows in the pool); --require-proxy was given"
        )
    if total:
        # Silently connecting directly would take on ban risk without
        # telling the user.
        log.warning("the pool has proxies but none are eligible; connecting directly", total=total)
    else:
        log.info("proxy pool is empty; connecting directly")
    return []


def build_plans(
    session: Session,
    eligible: Sequence[Proxy],
    settings: Settings,
    *,
    require_proxy: bool = False,
) -> list[ProxyPlan]:
    """Resolves eligible rows to endpoints, skipping what cannot be decrypted.

    An undecryptable password is reported, NOT marked dead (the key is wrong,
    not the pool). `require_proxy` is re-checked here for that case.
    """
    plans: list[ProxyPlan] = []
    for row in eligible:
        try:
            endpoint = endpoint_of(row, settings)
        except PasswordUndecryptable as exc:
            log.error("could not decrypt the proxy password", proxy=row.label, error=str(exc))
            continue
        plans.append(ProxyPlan(proxy_id=int(row.id), proxy_label=row.label, dsn=endpoint.dsn()))

    if require_proxy and eligible and not plans:
        raise NoEligibleProxy(
            f"none of the {len(eligible)} eligible proxies could be "
            "decrypted; --require-proxy was given "
            "(is YF_PROXY_SECRET_KEY correct?)"
        )
    return plans


def tracker_for(proxy_id: int | None, settings: Settings) -> ShardProxyTracker:
    """The health tracker for one assignment.

    A direct-connection shard still gets a tracker; it has no proxy row
    to attribute events to.
    """
    return ShardProxyTracker(proxy_id, ProxyPolicy.from_settings(settings))


def record_crashes(
    factory: sessionmaker[Session], plans: Sequence[ProxyPlan], settings: Settings
) -> None:
    """Marks a SHARD_CRASH against every proxy whose shard died."""
    policy = ProxyPolicy.from_settings(settings)
    now = datetime.now(UTC)
    with factory() as session:
        for plan in plans:
            log.error("shard terminated unexpectedly", proxy=plan.proxy_label)
            persist_event(
                session,
                plan.proxy_id,
                HealthEvent.SHARD_CRASH,
                policy=policy,
                now=now,
                error="shard terminated or timed out",
            )
        session.commit()
