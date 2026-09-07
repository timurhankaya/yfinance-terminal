"""What one symbol's work hands from the worker threads to the writer.

Its own module because both sides need it and neither owns it: the
worker threads fill it with no database open, the persist step drains it
inside one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yfin.core.errors import ErrorKind
from yfin.datasets.base import Dataset, NormalizedResult
from yfin.storage.changes import ChangeContext


@dataclass
class SymbolPayload:
    """Worker output: all normalized results for one symbol."""

    symbol: str
    resolved: bool
    # Carried to the write step: `--full-refresh` has to reach the gates,
    # not stop at the watermark. Zeroing the watermark alone refetched the
    # data and then let the hash gate call it unchanged, so the one failure
    # the flag exists for -- data rows lost, gate row intact -- was the one
    # it could not repair.
    full_refresh: bool = False
    results: list[tuple[Dataset[Any], NormalizedResult, int, int]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    # Datasets excluded before running (name, reason). A third channel is
    # needed because these are neither results nor errors; dropping them
    # silently would make datasets vanish from the audit on a --start run.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Datasets left out of scope (name, reason). Kept separate from
    # `skipped` because it maps to a different ItemStatus: skipped means
    # "excluded by content_hash/date_range", out_of_scope means "this
    # symbol was never targeted for this interval".
    out_of_scope: list[tuple[str, str]] = field(default_factory=list)
    # How this symbol's writes become change events, or None when
    # `yf_changes_enabled` is off. The CONTEXT travels here because only the
    # runner knows the run id; the COLLECTOR is built from it per attempt
    # inside `persist_with_retry`, so a transaction replayed after a lock
    # conflict produces one set of events rather than two.
    changes: ChangeContext | None = None
    error: str | None = None
    # For proxy health accounting. Processed on the consumer thread, so no
    # lock is needed on the tracker.
    error_kinds: list[ErrorKind] = field(default_factory=list)
    success_count: int = 0
