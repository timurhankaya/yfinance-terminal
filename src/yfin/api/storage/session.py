"""Database access for the API process.

One engine per process, built lazily. The pipeline builds an engine per
command; a long-lived server wants the pool to outlive the request, so
the factory is cached rather than recreated.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from yfin.core.config import get_settings
from yfin.storage.db import create_db_engine


@functools.lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_db_engine(get_settings(), application_name="yfin-api")


@functools.lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def session_scope() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed.

    Read endpoints do not commit; the token endpoint commits explicitly.
    """
    with get_session_factory()() as session:
        yield session
