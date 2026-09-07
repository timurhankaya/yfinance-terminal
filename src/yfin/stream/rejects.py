"""The vocabulary for "this was dropped, and why".

A leaf module on purpose: it imports nothing from `yfin` and nothing
heavy, so importing it loads neither yfinance, nor protobuf, nor
SQLAlchemy (measured).

`Reject` and `DecodeResult` are the whole stream package's currency --
`connection.py`, `supervisor.py` and `writer.py` all speak them -- and
they used to live in `protocol.py`, which imports yfinance for the
generated `PricingData` message. Depending on the decoder to obtain a
four-field dataclass was the wrong edge, and this module removes it.

**It does not make the package free of yfinance, and that was measured
rather than assumed.** `connection.py` needs `decode_envelope` to read a
frame, so it imports `protocol.py` for real; `supervisor.py` imports
`connection.py`; `writer.py` imports `supervisor.py` to drain its queue.
Every link in that chain is a functional dependency, so `writer.py` still
loads yfinance at import time. What changed is that it now does so
because of what it actually uses, not to name a reject.

The reason strings are `stream_rejects.reason` values.
`tests/unit/test_stream_schema.py` holds them against the database enum,
so adding one here without adding it to the migration fails the build.
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
    """One thing that was dropped, and why.

    Mirrors stream_rejects. A reject is not always fatal to the row:
    non_finite_field and field_out_of_range null one column and keep the
    rest, because losing 32 good fields over one bad one would be worse
    than the bad field.
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
