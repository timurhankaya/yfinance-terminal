"""Unit tests must reach no service outside this process.

The README states it and CI depends on it: the `check` job runs
`pytest -q` with no service container and no credentials, so a unit test
that quietly called Yahoo would pass locally and hang or fail there. A
claim nothing enforces is a comment, so this enforces it.

Loopback is allowed on purpose. `test_stream_connection.py` runs a real
websocket server in-process and talks to it over 127.0.0.1; that needs
no upstream, no database and no network the CI runner does not already
have. What the claim is actually about is leaving the machine.

The guard is on `connect`, not on the import: constructing a client, an
Engine or a Ticker stays fine. Only reaching out does not.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})


class ExternalNetworkUsed(RuntimeError):
    """A unit test tried to connect to something outside this machine."""


def _host_of(address: Any) -> str | None:
    """Host from an AF_INET / AF_INET6 address, or None for anything else.

    AF_UNIX addresses are `str`/`bytes` and never leave the machine, so
    they are not the guard's business.
    """
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fails the test that connects out, naming the address it asked for."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(address: Any) -> None:
        host = _host_of(address)
        if host is None or host in LOOPBACK or host.startswith("127."):
            return
        raise ExternalNetworkUsed(
            f"a unit test tried to connect to {address!r}. Unit tests reach "
            "nothing outside this process: mark it `repo` or `live`, or stub "
            "the call."
        )

    def guard(self: socket.socket, address: Any) -> Any:
        check(address)
        return real_connect(self, address)

    def guard_ex(self: socket.socket, address: Any) -> Any:
        check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_ex)
    yield
