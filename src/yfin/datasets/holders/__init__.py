"""Sahiplik/insider dataset'leri; import registry kayitlarini tetikler.

Alti dataset TEK bir Yahoo istegini paylasir (holders demeti, 7 modul).
Paylasilan istek PAYLASILAN KADER demektir: istek patarsa altisi da `failed`
olur. Bu dogru davranistir ve maliyeti yoktur -- yfinance sonucu (hatayi da)
`Ticker` uzerinde onbellege aldigi icin alti hucre TEK istek harcar (AH S8.1).
"""

from __future__ import annotations

from yfin.datasets.holders import (  # noqa: F401
    breakdown,
    insider_activity,
    insider_roster,
    insider_transactions,
    institutional,
)
