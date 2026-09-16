"""API clients, their secrets and their scopes. Secrets are a separate table
so a client can hold two live ones during rotation. `auth_epoch` makes
revocation immediate without a database read on verification: every
change bumps it, tokens carry the value they were minted with and are
rejected once they fall behind."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.api.models.plans import PLAN_NAME_LENGTH, _enum
from yfin.core.families import DataFamily, scope_for
from yfin.models.base import Base, TsType

#: "yfc_" + token_urlsafe(24) -> 4 + 32 characters.
CLIENT_ID_LENGTH = 36
CLIENT_ID_PREFIX = "yfc_"

CLIENT_NAME_LENGTH = 64
#: RFC 5321 maximum.
OWNER_EMAIL_LENGTH = 254
#: An argon2id encoded hash is ~97 chars at our parameters; 255 leaves
#: room to raise them without a migration.
SECRET_HASH_LENGTH = 255


ApiScope = enum.StrEnum(  # type: ignore[misc]
    "ApiScope", {f.name: scope_for(f) for f in DataFamily}
)
"""Scopes, derived from the family set rather than retyped.

Retyping them would let the two lists drift, and a scope that exists in
the enum but in no family would be unreachable -- or worse, a family
with no scope would be silently open.
"""


class ApiClient(Base):
    __tablename__ = "api_clients"

    client_id: Mapped[str] = mapped_column(
        String(CLIENT_ID_LENGTH, collation="C"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(CLIENT_NAME_LENGTH, collation="C"), nullable=False)
    # PII: never appears in a /v1 response, an error body or a log line.
    owner_email: Mapped[str] = mapped_column(
        String(OWNER_EMAIL_LENGTH, collation="C"), nullable=False
    )
    plan: Mapped[str] = mapped_column(
        String(PLAN_NAME_LENGTH, collation="C"),
        ForeignKey("api_plans.plan", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Bumped on secret revocation, scope change, plan change and disable.
    auth_epoch: Mapped[int] = mapped_column(
        Integer,
        CheckConstraint('"auth_epoch" >= 0', name="ck_api_clients_epoch_nonneg"),
        nullable=False,
        server_default=text("0"),
    )
    # Written from a Redis buffer about once a minute, not per request:
    # an UPDATE on every call would put a hot row in the request path.
    last_used_at: Mapped[datetime | None] = mapped_column(TsType())
    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(TsType())


class ApiClientSecret(Base):
    """One row per issued secret; only the hash is stored. The "at most two
    live secrets" cap is enforced in the repository: no unique index can
    express a per-parent count."""

    __tablename__ = "api_client_secrets"
    __table_args__ = (
        UniqueConstraint("secret_hash", name="uq_api_client_secrets_hash"),
        Index(
            "ix_api_client_secrets_live",
            "client_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(CLIENT_ID_LENGTH, collation="C"),
        ForeignKey("api_clients.client_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    secret_hash: Mapped[str] = mapped_column(
        String(SECRET_HASH_LENGTH, collation="C"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    # Set when a rotation gives the old secret a deadline.
    expires_at: Mapped[datetime | None] = mapped_column(TsType())
    revoked_at: Mapped[datetime | None] = mapped_column(TsType())


class ApiClientScope(Base):
    __tablename__ = "api_client_scopes"

    client_id: Mapped[str] = mapped_column(
        String(CLIENT_ID_LENGTH, collation="C"),
        ForeignKey("api_clients.client_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    scope: Mapped[str] = mapped_column(_enum(ApiScope, "api_scope"), primary_key=True)
