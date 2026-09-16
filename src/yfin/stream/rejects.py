"""The vocabulary for "this was dropped, and why".

A leaf module: no `yfin` or heavy imports, so naming a reject costs
nothing. The reason strings are `stream_rejects.reason` enum values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

# --- reject reasons (stream_rejects.reason) --------------------------------

REJECT_DECODE_FAILED: Final = "decode_failed"
REJECT_NO_TIMESTAMP: Final = "no_timestamp"
REJECT_UNKNOWN_SYMBOL: Final = "unknown_symbol"
REJECT_SYMBOL_TOO_LONG: Final = "symbol_too_long"
REJECT_NON_FINITE: Final = "non_finite_field"
REJECT_OUT_OF_RANGE: Final = "field_out_of_range"
REJECT_EXPIRE_DATE_RANGE: Final = "expire_date_range"
REJECT_MALFORMED_SUBSCRIPTION: Final = "malformed_subscription"


@dataclass(frozen=True)
class Reject:
    """One thing that was dropped, and why. Mirrors stream_rejects.

    Not always fatal to the row: non_finite_field and field_out_of_range
    null one column and keep the rest.
    """

    reason: str
    symbol: str | None = None
    detail: str | None = None
    raw_base64: str | None = None


@dataclass
class DecodeResult:
    """A decoded message: at most one row, plus whatever was dropped."""

    row: dict[str, Any] | None = None
    rejects: list[Reject] = field(default_factory=list)
