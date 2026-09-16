"""Single-proxy setup for the one-process runners.

The seam between the runners and `yfin.proxy`; the selection policy
itself lives in `proxy_plan.py`.
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

    Falls back to a direct connection when nothing is eligible or
    decryptable; these runners have no `require_proxy` flag.
    """
    with factory() as session:
        for plan in build_plans(select_eligible(session, limit=1), settings):
            configure_yfinance(
                plan.dsn, proxy_key=f"proxy-{plan.proxy_id}", settings=settings
            )
            log.info(f"{label} sync proxy", proxy=plan.proxy_label)
            return plan.proxy_id, plan.proxy_label, tracker_for(plan.proxy_id, settings)
    configure_yfinance(None, proxy_key="direct", settings=settings)
    log.info(f"{label} sync is connecting directly")
    return None, None, None


