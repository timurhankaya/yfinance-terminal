"""Single-proxy setup for the one-process runners.

`runner.py` deliberately does not import `yfin.proxy` -- the
`ProxyTracker` protocol exists so the symbol runner stays independent of
the proxy package. This module is the seam where the two meet, so the
market and domain runners can share the policy without dragging the
proxy package into the runner.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from yfin.core.config import Settings
from yfin.core.logging_setup import get_logger
from yfin.ingest.client import configure_yfinance
from yfin.proxy import (
    PasswordUndecryptable,
    ProxyPolicy,
    ShardProxyTracker,
    endpoint_of,
    select_eligible,
)

log = get_logger(__name__)


def setup_single_proxy(
    factory: sessionmaker[Any], settings: Settings, *, label: str
) -> tuple[int | None, str | None, ShardProxyTracker | None]:
    """Pick one proxy from the pool and point yfinance at it.

    The market and domain runners each ran one process, so neither shards
    the pool the way the symbol side does -- they take a single proxy or
    connect directly. Both had a verbatim copy of this, differing only in
    two log strings, which meant proxy-selection policy had two homes and
    a fix to one silently missed the other. `label` is that difference.

    Falls back to a direct connection when nothing is eligible. A proxy
    whose password cannot be decrypted is skipped and reported, NOT marked
    dead: the pool is fine, the operator's key is not.
    """
    with factory() as session:
        for row in select_eligible(session, limit=1):
            try:
                endpoint = endpoint_of(row, settings)
            except PasswordUndecryptable as exc:
                log.error("could not decrypt the proxy password", proxy=row.label, error=str(exc))
                continue
            configure_yfinance(endpoint.dsn(), proxy_key=f"proxy-{row.id}", settings=settings)
            log.info(f"{label} sync proxy", proxy=row.label)
            return (
                int(row.id),
                row.label,
                ShardProxyTracker(int(row.id), ProxyPolicy.from_settings(settings)),
            )
    configure_yfinance(None, proxy_key="direct", settings=settings)
    log.info(f"{label} sync is connecting directly")
    return None, None, None


