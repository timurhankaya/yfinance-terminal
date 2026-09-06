"""Proxy address: parsing, validation and a SAFE representation.

A pure value object; it touches neither the database nor the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from yfin.core.logging_setup import scrub
from yfin.models import ProxyScheme


@dataclass(frozen=True)
class ProxyEndpoint:
    """A decrypted proxy address. The password lives in memory ONLY."""

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

    def __repr__(self) -> str:  # keep the password out of repr() too
        return f"ProxyEndpoint({self.redacted()})"

    __str__ = __repr__


def parse_dsn(url: str) -> ProxyEndpoint:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"invalid proxy address: {scrub(url)}")
    try:
        scheme = ProxyScheme(parts.scheme.lower())
    except ValueError:
        valid = ", ".join(s.value for s in ProxyScheme)
        raise ValueError(f"unsupported scheme: {parts.scheme} (valid: {valid})") from None
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
