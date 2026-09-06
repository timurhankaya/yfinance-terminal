"""Test altyapisinin kendisi: surece ozel sema (S9.2 notu).

Bu mantik sessizce bozulursa iki es zamanli pytest kosusu yine birbirinin
tablolarini dusurur ve hata KODDA aranir; bu yuzden test edilir.
"""

from __future__ import annotations

import os

from helpers import pid_is_alive, schema_name


def test_schema_name_is_process_scoped() -> None:
    name = schema_name("yfinance_test")
    assert name == f"yfinance_test_{os.getpid()}"
    assert name != "yfinance_test"  # taban sema ASLA kullanilmaz


def test_current_process_is_alive() -> None:
    assert pid_is_alive(os.getpid())


def test_unused_pid_is_not_alive() -> None:
    """Temizlik yalnizca OLU PID'lerin semasini dusurur."""
    assert not pid_is_alive(999_999)
