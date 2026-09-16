"""proxies table: proxy pool, enablement, and health.

`is_enabled` is the operator's decision and the system never changes it;
`health` is the system's observation, cleared only by `proxy reset`. One
column would let ban detection re-enable a deliberately disabled proxy."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from yfin.models.base import (
    Base,
    HostType,
    ProxyLabelType,
    TsType,
)


class ProxyScheme(enum.StrEnum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"
    SOCKS5H = "socks5h"


class ProxyHealth(enum.StrEnum):
    UNKNOWN = "unknown"  # Never tried; eligible.
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    DEAD = "dead"  # Only `proxy reset` brings it back.


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """Project convention: writes the enum VALUES, not the Python names.

    `name` is explicit: SQLAlchemy would otherwise derive a non-snake_case
    name from the class, and on PostgreSQL it is a permanent type name."""
    return Enum(cls, values_callable=lambda e: [m.value for m in e], name=name)


class Proxy(Base):
    __tablename__ = "proxies"
    __table_args__ = (
        # This constraint actually holds because username is
        # NOT NULL DEFAULT '': PostgreSQL does not enforce UNIQUE across
        # rows containing NULL, so a nullable username would let the same
        # proxy be added repeatedly.
        UniqueConstraint("scheme", "host", "port", "username", name="uq_proxies_endpoint"),
        Index("ix_proxies_eligibility", "is_enabled", "health"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    label: Mapped[str] = mapped_column(ProxyLabelType(), nullable=False, unique=True)

    scheme: Mapped[ProxyScheme] = mapped_column(_enum(ProxyScheme, "proxy_scheme"), nullable=False)
    host: Mapped[str] = mapped_column(HostType(), nullable=False)
    port: Mapped[int] = mapped_column(
        Integer,
        CheckConstraint('"port" BETWEEN 1 AND 65535', name="ck_proxies_port_range"),
        nullable=False,
    )
    # '' = no username. Not NULL: see the __table_args__ comment above.
    username: Mapped[str] = mapped_column(
        ProxyLabelType(), nullable=False, server_default=text("''")
    )
    # Fernet token bytes, never text.
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    health: Mapped[ProxyHealth] = mapped_column(
        _enum(ProxyHealth, "proxy_health"), nullable=False, server_default=ProxyHealth.UNKNOWN.value
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(TsType())

    # Consecutive failures; past the threshold triggers cooldown, then resets to 0.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Cumulative cooldown rounds; past the threshold, dead. A success does
    # not reset this -- otherwise a single success mid-decline would
    # restart the path to dead every time, keeping a half-dead proxy in
    # the pool forever.
    cooldown_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    success_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    failure_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    last_ok_at: Mapped[datetime | None] = mapped_column(TsType())
    last_error_at: Mapped[datetime | None] = mapped_column(TsType())
    last_checked_at: Mapped[datetime | None] = mapped_column(TsType())
    # ErrorKind + a redacted message; the password never enters this field.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(),
        nullable=False,
        server_default=func.now(),
        # PostgreSQL has no `ON UPDATE CURRENT_TIMESTAMP` clause, and a
        # trigger would hide the behavior in the schema. `onupdate` applies
        # to ORM flush and Core `update()` only: a raw-SQL writer must set
        # `updated_at` explicitly.
        onupdate=lambda: datetime.now(UTC),
    )

    def endpoint(self) -> str:
        """host:port. Carries no credentials; used in logs."""
        return f"{self.host}:{self.port}"
