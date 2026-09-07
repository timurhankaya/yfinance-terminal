"""Abstractions the pipeline's runners share.

`ProxyTracker` lived in `runner.py`, which made every other runner import
the symbol runner to reach it: `turn.py`, `market_runner.py` and
`domain_runner.py` all did, and none of them has anything to do with the
symbol path's worker threads or sharding. The abstraction was pulling its
own consumers onto the heaviest module in the package.

Here it depends on nothing but the error taxonomy and a Session, so the
direction of every import is now runner -> contract rather than
contract -> runner. `storage/contracts.py` holds the write-side
protocols for the same reason and under the same name.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from yfin.core.errors import ErrorKind


class ProxyTracker(Protocol):
    """The only interface a runner sees into proxy health accounting.

    The concrete implementation is `yfin.proxy.ShardProxyTracker`; the
    runners depend on this abstraction instead so proxy policy can evolve
    without touching them, and tests can pass a fake tracker.
    """

    withdrawn: bool

    def record_success(self) -> None: ...

    def record_error(self, kind: ErrorKind, message: str | None = None) -> None: ...

    def flush(self, session: Session) -> None: ...
