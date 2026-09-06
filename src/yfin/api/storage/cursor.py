"""Keyset pagination cursors.

`OFFSET` is not used anywhere in this API. On a hypertable with millions
of rows deep pages degrade linearly, and under concurrent writes an
offset silently skips rows -- a client paging through a symbol's history
would end up with gaps it has no way to detect.

A cursor is therefore the sort key of the last row returned, and it
carries two things besides:

`v` is a schema version. If a sort key ever changes shape, old cursors
must fail loudly rather than be reinterpreted against the new one.

`q` is a fingerprint of every other parameter of the request. Without it
a cursor from `interval=1d` could be handed to `interval=1m`, which
resolves to a different table with a different key -- either a 500 or,
worse, a silently wrong and very expensive scan. With it, the mismatch is
a 422 the client can act on.

The cursor is NOT signed. Authorisation comes from the scope and the path,
never from the cursor, and the data behind it is not tenant-specific: a
forged cursor only jumps to another point in a query the caller was
already allowed to make. Signing would add key management for no threat
that exists here. What it does need is validation -- the fingerprint and
typed parsing above -- because the failure it prevents is a broken query,
not an unauthorised one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import date, datetime
from typing import Any

CURSOR_VERSION = 1

#: Long enough to make collisions irrelevant, short enough to keep the
#: cursor small.
FINGERPRINT_LENGTH = 16


class InvalidCursor(Exception):
    """The cursor is unusable. Always a 422, never a 500."""


def fingerprint(parts: dict[str, Any]) -> str:
    """A stable digest of everything that is not the cursor itself."""
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def _encode_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__t__": "dt", "v": value.isoformat()}
    if isinstance(value, date):
        return {"__t__": "d", "v": value.isoformat()}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and "__t__" in value:
        kind = value.get("__t__")
        raw = value.get("v")
        if not isinstance(raw, str):
            raise InvalidCursor("malformed key value")
        try:
            return datetime.fromisoformat(raw) if kind == "dt" else date.fromisoformat(raw)
        except ValueError as exc:
            raise InvalidCursor("malformed key value") from exc
    return value


def encode(key: tuple[Any, ...], *, query: dict[str, Any]) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "q": fingerprint(query),
        "k": [_encode_value(part) for part in key],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(cursor: str, *, query: dict[str, Any], arity: int) -> tuple[Any, ...]:
    """Returns the sort key, or raises InvalidCursor.

    Every failure mode lands here as one exception type, so a malformed
    cursor can never reach the query builder and become a 500.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("not a cursor") from exc

    if not isinstance(payload, dict):
        raise InvalidCursor("not a cursor")
    if payload.get("v") != CURSOR_VERSION:
        raise InvalidCursor("cursor is from an older format")
    if payload.get("q") != fingerprint(query):
        raise InvalidCursor("cursor does not belong to this query")

    key = payload.get("k")
    if not isinstance(key, list) or len(key) != arity:
        raise InvalidCursor("cursor key has the wrong shape")
    return tuple(_decode_value(part) for part in key)
