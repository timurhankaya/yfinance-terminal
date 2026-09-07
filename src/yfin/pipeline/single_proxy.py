"""Single-proxy setup for the one-process runners.

No runner imports `yfin.proxy` -- the `ProxyTracker` protocol in
`pipeline/contracts.py` exists so they stay independent of the proxy
package. This module is the seam where the two meet, so the market and
domain runners can share the policy without dragging the proxy package
into a runner.

The selection policy itself lives in `proxy_plan.py`, which the symbol
side's sharding uses too. This module is the N=1 caller of it, not a
second implementation -- which is the whole point, given that the
duplication this function was written to remove had grown back in
`shard.py`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from yfin.core.config import Settings
from yfin.core.logging_setup import get_logger
from yfin.ingest.client import configure_yfinance
from yfin.pipeline.proxy_plan import build_plans, tracker_for
from yfin.proxy import ShardProxyTracker, select_eligible

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

    Falls back to a direct connection when nothing is eligible. The
    decryption-skip policy is `proxy_plan.build_plans`', not a second copy
    of it: an undecryptable proxy is skipped and reported, NOT marked dead
    (the pool is fine, the operator's key is not), and that rule now has
    exactly one implementation for both the one-proxy and the N-proxy
    paths. `require_proxy` is not passed -- these runners have no such
    flag and fall back to a direct connection instead.
    """
    with factory() as session:
        for plan in build_plans(session, select_eligible(session, limit=1), settings):
            configure_yfinance(
                plan.dsn, proxy_key=f"proxy-{plan.proxy_id}", settings=settings
            )
            log.info(f"{label} sync proxy", proxy=plan.proxy_label)
            return plan.proxy_id, plan.proxy_label, tracker_for(plan.proxy_id, settings)
    configure_yfinance(None, proxy_key="direct", settings=settings)
    log.info(f"{label} sync is connecting directly")
    return None, None, None


