"""Proxy parolasinin Fernet ile sifrelenmesi (P3.4).

Parola DB'ye DUZ METIN yazilmaz; anahtar .env'deki YF_PROXY_SECRET_KEY'dir.
Boylece bir DB dokumu tek basina kullanilamaz.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from yfin.config import Settings, get_settings
from yfin.models import Proxy, ProxyScheme
from yfin.proxy.dsn import ProxyEndpoint


class SecretKeyMissing(RuntimeError):
    """YF_PROXY_SECRET_KEY yok; parolali proxy eklenemez/cozulemez."""


class PasswordUndecryptable(RuntimeError):
    """Token mevcut anahtarla cozulemedi (anahtar degismis olabilir)."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.yf_proxy_secret_key.strip()
    if not key:
        raise SecretKeyMissing(
            "YF_PROXY_SECRET_KEY tanimli degil; parolali proxy eklenemez. "
            "Anahtar uretmek icin: python -c "
            "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode())


def encrypt_password(plain: str, settings: Settings | None = None) -> bytes | None:
    if not plain:
        return None
    return _fernet(settings or get_settings()).encrypt(plain.encode())


def decrypt_password(token: bytes | None, settings: Settings | None = None) -> str:
    if not token:
        return ""
    try:
        return _fernet(settings or get_settings()).decrypt(token).decode()
    except InvalidToken as exc:
        raise PasswordUndecryptable(
            "proxy parolasi cozulemedi; YF_PROXY_SECRET_KEY degismis olabilir"
        ) from exc


def endpoint_of(row: Proxy, settings: Settings | None = None) -> ProxyEndpoint:
    return ProxyEndpoint(
        scheme=ProxyScheme(row.scheme),
        host=row.host,
        port=row.port,
        username=row.username,
        password=decrypt_password(row.password_enc, settings),
    )


# --------------------------------------------------------------------------
# Saglik durum makinesi (saf)
# --------------------------------------------------------------------------
