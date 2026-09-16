"""Abstractions the pipeline's runners share.

Depends only on the error taxonomy and a Session, so imports run
runner -> contract and never the other way.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from yfin.core.errors import ErrorKind


class ProxyTracker(Protocol):
    """The only interface a runner sees into proxy health accounting.

    Implemented by `yfin.proxy.ShardProxyTracker`; tests pass a fake.
    """

    withdrawn: bool

    def record_success(self) -> None: ...

    def record_error(self, kind: ErrorKind, message: str | None = None) -> None: ...

    def flush(self, session: Session) -> None: ...
