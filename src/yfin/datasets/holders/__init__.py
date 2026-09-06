"""Ownership/insider datasets; importing triggers registry registration.

Six datasets SHARE ONE Yahoo request (the holders bundle, 7 modules). A
shared request means SHARED FATE: if the request fails, all six become
`failed`. This is correct and free -- yfinance caches the result (and the
error) on the `Ticker`, so the six cells together cost ONE request.
"""

from __future__ import annotations

from yfin.datasets.holders import (  # noqa: F401
    breakdown,
    insider_activity,
    insider_roster,
    insider_transactions,
    institutional,
)
