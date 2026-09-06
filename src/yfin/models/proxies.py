"""proxies tablosu: proxy havuzu, aktiflik ve saglik durumu (P3.1).

Aktiflik IKI kolondur ve bu bilinclidir:

  * `is_enabled` OPERATORUN kararidir; sistem asla degistirmez.
  * `health`     SISTEMIN gozlemidir; CLI yalnizca `proxy reset` ile sifirlar.

Tek bir `is_active` kolonu ikisini karistirirdi: otomatik ban tespiti,
operatorun bilerek kapattigi bir proxy'yi basarili bir istek sonrasi
yeniden acabilirdi. Ayni ayrim symbols tablosunda da var
(`is_active` kullanici karari, `unknown_streak` sistem sayaci).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Enum, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT, SMALLINT, VARBINARY
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import FetchedValue

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
    UNKNOWN = "unknown"  # hic denenmedi; UYGUNDUR
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    DEAD = "dead"  # yalnizca `proxy reset` geri getirir


def _enum(cls: type[enum.StrEnum]) -> Enum:
    """Mevcut konvansiyon: SQLAlchemy enum ISIMLERINI degil DEGERLERINI yazar."""
    return Enum(cls, values_callable=lambda e: [m.value for m in e])


class Proxy(Base):
    __tablename__ = "proxies"
    __table_args__ = (
        # username NOT NULL DEFAULT '' oldugu icin bu kisit GERCEKTEN
        # uygulanir: MySQL, NULL iceren satirlarda UNIQUE tekilligi
        # zorlamaz ve nullable bir username ayni proxy'nin defalarca
        # eklenmesine izin verirdi.
        UniqueConstraint("scheme", "host", "port", "username", name="uq_proxies_endpoint"),
        Index("ix_proxies_eligibility", "is_enabled", "health"),
    )

    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(ProxyLabelType(), nullable=False, unique=True)

    scheme: Mapped[ProxyScheme] = mapped_column(_enum(ProxyScheme), nullable=False)
    host: Mapped[str] = mapped_column(HostType(), nullable=False)
    port: Mapped[int] = mapped_column(SMALLINT(unsigned=True), nullable=False)
    # '' = kullanicisiz. NULL DEGIL: bkz. __table_args__ yorumu.
    username: Mapped[str] = mapped_column(
        ProxyLabelType(), nullable=False, server_default=text("''")
    )
    # Fernet token'i URL-safe base64 ASCII'dir; VARBINARY charset/collation
    # donusum riskini sifirlar. 512 bayt ~310 baytlik parolaya kadar yeter.
    password_enc: Mapped[bytes | None] = mapped_column(VARBINARY(512))

    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    health: Mapped[ProxyHealth] = mapped_column(
        _enum(ProxyHealth), nullable=False, server_default=ProxyHealth.UNKNOWN.value
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(TsType())

    # ardisik hata; esigi asinca cooldown, sonra 0'a doner
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # KUMULATIF cooldown turu; esigi asinca dead. Basari bunu SIFIRLAMAZ,
    # aksi halde arada tek bir basari dead'e giden yolu surekli bastan
    # baslatir ve yari-olu bir proxy sonsuza dek havuzda kalirdi.
    cooldown_rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    success_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    failure_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    last_ok_at: Mapped[datetime | None] = mapped_column(TsType())
    last_error_at: Mapped[datetime | None] = mapped_column(TsType())
    last_checked_at: Mapped[datetime | None] = mapped_column(TsType())
    # ErrorKind + REDAKTE EDILMIS mesaj; parola asla girmez
    last_error: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        TsType(), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TsType(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
        server_onupdate=FetchedValue(),
    )

    def endpoint(self) -> str:
        """host:port. Kimlik bilgisi ICERMEZ; loglarda bu kullanilir."""
        return f"{self.host}:{self.port}"
