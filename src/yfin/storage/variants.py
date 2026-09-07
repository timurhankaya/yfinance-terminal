"""Stored variant state, read on the storage side of the contract.

Implements `VariantState` so the dataset layer can ask which screens are
switched off without importing SQLAlchemy or holding a Session.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from yfin.models.discovery import Screen


class ScreenVariantState:
    """`VariantState` backed by the `screens` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def disabled_variants(self) -> frozenset[str]:
        stmt = select(Screen.screen_key).where(Screen.is_enabled.is_(False))
        return frozenset(self._session.execute(stmt).scalars())
