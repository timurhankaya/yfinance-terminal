"""Ownership/insider datasets; importing triggers registry registration.

Six datasets share one Yahoo request (cached on the `Ticker`), so if it
fails all six cells become `failed`.
"""

from __future__ import annotations

from yfin.datasets.holders import (  # noqa: F401
    breakdown,
    insider_activity,
    insider_roster,
    insider_transactions,
    institutional,
)
