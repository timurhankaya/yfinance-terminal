"""Unit tests must not leave the machine: CI's `check` job runs them with no services or
credentials. Loopback is allowed (`test_stream_connection.py` runs an in-process websocket
server). The guard is on `connect`, not on construction."""

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
