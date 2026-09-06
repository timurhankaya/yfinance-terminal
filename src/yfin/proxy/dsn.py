"""Proxy adresi: parse, dogrulama ve GUVENLI temsil.

Saf deger nesnesi; DB'ye de aga da dokunmaz.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from yfin.logging_setup import scrub
from yfin.models import ProxyScheme


@dataclass(frozen=True)
class ProxyEndpoint:
    """Cozulmus proxy adresi. Parola YALNIZCA bellekte bulunur."""

    scheme: ProxyScheme
    host: str
    port: int
    username: str = ""
    password: str = ""

    def dsn(self) -> str:
        """curl/libcurl'e verilecek tam adres. ASLA loglanmaz."""
        auth = ""
        if self.username:
            auth = quote(self.username, safe="")
            if self.password:
                auth += f":{quote(self.password, safe='')}"
            auth += "@"
        return f"{self.scheme.value}://{auth}{self.host}:{self.port}"

    def redacted(self) -> str:
        return scrub(self.dsn())

    def __repr__(self) -> str:  # parola repr'e de sizmasin
        return f"ProxyEndpoint({self.redacted()})"

    __str__ = __repr__


def parse_dsn(url: str) -> ProxyEndpoint:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"gecersiz proxy adresi: {scrub(url)}")
    try:
        scheme = ProxyScheme(parts.scheme.lower())
    except ValueError:
        valid = ", ".join(s.value for s in ProxyScheme)
        raise ValueError(f"desteklenmeyen sema: {parts.scheme} (gecerli: {valid})") from None
    if parts.port is None:
        raise ValueError("port zorunludur")
    return ProxyEndpoint(
        scheme=scheme,
        host=parts.hostname,
        port=parts.port,
        username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )


def default_label(endpoint: ProxyEndpoint) -> str:
    return f"{endpoint.host}-{endpoint.port}".replace(".", "-").replace(":", "-")[:64]


# --------------------------------------------------------------------------
# Parola sifreleme
# --------------------------------------------------------------------------
