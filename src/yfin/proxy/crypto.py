"""Proxy password encryption via Fernet.

Passwords are never stored in plaintext; the key is YF_PROXY_SECRET_KEY
in .env, so a DB dump alone is not enough to recover them.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from yfin.core.config import Settings, get_settings
from yfin.models import Proxy, ProxyScheme
from yfin.proxy.dsn import ProxyEndpoint


class SecretKeyMissing(RuntimeError):
    """YF_PROXY_SECRET_KEY is unset; a password-protected proxy can't be added or decrypted."""


class PasswordUndecryptable(RuntimeError):
    """Token could not be decrypted with the current key (key may have changed)."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.yf_proxy_secret_key.strip()
    if not key:
        raise SecretKeyMissing(
            "YF_PROXY_SECRET_KEY is not set; cannot add a password-protected proxy. "
            "Generate a key with: python -c "
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
            "could not decrypt proxy password; YF_PROXY_SECRET_KEY may have changed"
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
# Health state machine (pure)
# --------------------------------------------------------------------------
