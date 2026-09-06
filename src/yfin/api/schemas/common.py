"""Response envelopes and the shared query parameters.

The envelope is two shapes, not one. A collection carries `next_cursor`;
a single resource does not, and pretending otherwise would leave every
client checking a field that is structurally always null.

`as_of` says when the data was last verified against the source, and it
is null wherever the schema does not record that. It is deliberately not
filled in with the newest row's timestamp: data time and fetch time are
different questions, and this project has already paid for confusing them
once (see the session_date / local_date note in models/bars.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 100


class Collection(BaseModel, Generic[T]):
    data: list[T]
    next_cursor: str | None = Field(
        default=None,
        description="Pass as `cursor` to fetch the next page. Null on the last page.",
    )
    as_of: datetime | None = Field(
        default=None,
        description=(
            "When this data was last verified against the source. Null where the "
            "schema keeps no fetch timestamp -- the price tables deliberately do "
            "not, since a per-row timestamp would cost gigabytes to answer a "
            "question the bar's own ts_utc already covers."
        ),
    )


class Resource(BaseModel, Generic[T]):
    data: T
    as_of: datetime | None = None


class Problem(BaseModel):
    """RFC 9457. The only error shape outside /oauth/token."""

    type: str
    title: str
    status: int
    detail: str | None = None
    request_id: str | None = None
