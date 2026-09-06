"""Fixture loading and rehydration helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


# --- process-specific test schema ------------------------------------------


def schema_name(base: str) -> str:
    """Test schema is process-specific: `<base>_<pid>`.

    With one fixed schema, two pytest runs (e.g. two editor sessions)
    would `drop_all` each other's tables, showing up as concurrent DDL
    errors and non-reproducible row-count mismatches. Tying the schema
    name to the process rules this out.

    Uses a schema, not a database: `CREATE DATABASE` must run outside a
    transaction, copies the template database, and needs `CREATE
    EXTENSION timescaledb` in every new database; `CREATE SCHEMA` is
    ordinary DDL and `DROP SCHEMA ... CASCADE` also cleans up chunks.
    """
    return f"{base}_{os.getpid()}"


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's process: treat as alive
        return True
    return True


def drop_stale_schemas(engine: Engine, base: str) -> list[str]:
    """Drops leftover `<base>_<pid>` schemas from interrupted runs.

    Ctrl-C and crashes skip teardown; without this cleanup, schemas would
    accumulate indefinitely. Only schemas whose PID is no longer alive
    are dropped, so a concurrent run's schema is untouched. The base name
    itself (`<base>`, no numeric suffix) is also preserved.

    `engine` must be bound to the test database, not the bootstrap
    (`postgres`) connection: `information_schema.schemata` is
    database-specific, and the bootstrap connection can't see schemas in
    the test database. With the wrong engine, cleanup silently does
    nothing and schemas accumulate forever.
    """
    prefix = f"{base}_"
    dropped: list[str] = []
    with engine.connect() as conn:
        names = list(
            conn.execute(text("SELECT schema_name FROM information_schema.schemata")).scalars()
        )
        for name in names:
            suffix = name[len(prefix) :] if name.startswith(prefix) else ""
            if not suffix.isdigit() or pid_is_alive(int(suffix)):
                continue
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            conn.commit()
            dropped.append(name)
    return dropped


def load_fixture(symbol: str, dataset: str) -> Any:
    path = FIXTURE_ROOT / symbol / f"{dataset}.json"
    if not path.exists():
        pytest.skip(f"missing fixture: {path} (run scripts/capture_fixtures.py)")
    return json.loads(path.read_text(encoding="utf-8"))


def load_domain_fixture(kind: str, key: str, region: str = "US") -> Any:
    """Returns the raw domain envelope ({"data": {...}}).

    The envelope is not unwrapped: `fetch_domain` reads `payload["data"]`
    and tests must go through the same path.
    """
    suffix = "" if region == "US" else f".{region}"
    path = FIXTURE_ROOT / "_domain" / kind / f"{key}{suffix}.json"
    if not path.exists():
        pytest.skip(
            f"missing fixture: {path} "
            "(run python scripts/capture_fixtures.py --domain)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def domain_data(kind: str, key: str, region: str = "US") -> Any:
    return load_domain_fixture(kind, key, region)["data"]


def domain_fixture_keys(kind: str) -> list[tuple[str, str]]:
    """List of (key, region) for the available domain fixtures."""
    root = FIXTURE_ROOT / "_domain" / kind
    if not root.exists():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.json")):
        stem = path.stem
        key, _, region = stem.partition(".")
        out.append((key, region or "US"))
    return out


# --- fixture rehydration ----------------------------------------------------


def as_series(records: Any) -> Any:
    """Converts capture_fixtures.py's [{index, value}] shape to a Series."""
    import pandas as pd

    if records is None:
        return None
    if not records:
        return pd.Series([], dtype=object)
    # Offsets are mixed due to DST transitions (-05:00 / -04:00); forcing a
    # single DatetimeIndex would lose tz info. The real API returns a
    # tz-aware index and normalize handles each element individually.
    index = pd.Index([pd.Timestamp(r["index"]) for r in records], dtype=object)
    return pd.Series([r["value"] for r in records], index=index)


def as_frame(records: Any) -> Any:
    """Converts a history fixture to a DataFrame (index = Date)."""
    import pandas as pd

    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    index_col = "index" if "index" in frame.columns else "Date"
    index = pd.Index([pd.Timestamp(v) for v in frame[index_col]], dtype=object)
    frame = frame.drop(columns=[index_col])
    frame.index = index
    return frame


def as_statement_frame(records: Any) -> Any:
    """Converts a financial statement fixture to a DataFrame.

    The real API returns a str line-item label as index and period-end
    (tz-naive Timestamp) columns; the fixture preserves that shape.
    """
    import pandas as pd

    if not records:
        return pd.DataFrame()
    labels = [r["index"] for r in records]
    columns = [c for c in records[0] if c != "index"]
    data = {pd.Timestamp(col): [r.get(col) for r in records] for col in columns}
    return pd.DataFrame(data, index=pd.Index(labels, dtype=str))


def as_valuation_frame(records: Any) -> Any:
    """Valuation fixture: column labels stay raw.

    Differs from `as_statement_frame`: the source returns 'Current' and
    'M/D/YYYY' strings, not period-end Timestamps. Converting columns to
    Timestamp here would skip the very step (`period_columns`) the
    dataset is meant to exercise.
    """
    import pandas as pd

    if not records:
        return pd.DataFrame()
    labels = [r["index"] for r in records]
    columns = [c for c in records[0] if c != "index"]
    data = {col: [r.get(col) for r in records] for col in columns}
    return pd.DataFrame(data, index=pd.Index(labels, dtype=str))


def as_calendar_frame(records: Any) -> Any:
    """Calendar fixture: index is symbol/event name, columns keep raw names."""
    import pandas as pd

    if not records:
        return pd.DataFrame()
    index = pd.Index([r["index"] for r in records], dtype=object)
    columns = [c for c in records[0] if c != "index"]
    frame = pd.DataFrame({c: [r.get(c) for r in records] for c in columns}, index=index)
    return frame


def as_earnings_frame(payload: Any) -> Any:
    """earnings_dates fixture: {"tz": ..., "records": [...]}.

    The real API returns a tz-aware DatetimeIndex, and the tz name matters
    (even THYAO.IS uses America/New_York); an ISO string only carries the
    offset.
    """
    import pandas as pd

    records = payload.get("records") if isinstance(payload, dict) else payload
    if not records:
        return None
    # ISO strings carry mixed offsets (EDT/EST); normalize to UTC first
    # with utc=True, then convert to the source tz.
    index = pd.DatetimeIndex(pd.to_datetime([r["index"] for r in records], utc=True))
    tz = payload.get("tz") if isinstance(payload, dict) else None
    if tz:
        index = index.tz_convert(tz)
    columns = [c for c in records[0] if c != "index"]
    return pd.DataFrame({c: [r.get(c) for r in records] for c in columns}, index=index)


# --- analysis / holders / funds fixtures ------------------------------------

_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}.*)?$")


def _revive(value: Any) -> Any:
    """Converts an ISO timestamp back into a Timestamp.

    Fixtures are JSON, where `Timestamp` becomes a string; the real API
    returns `Timestamp`. Without this conversion the test would run
    against an input shape that never occurs in production -- passing
    without proving anything.
    """
    if isinstance(value, str) and _ISO_TIMESTAMP.match(value):
        import pandas as pd

        try:
            return pd.Timestamp(value)
        except ValueError:
            return value
    return value


def as_dataset_frame(records: Any, *, datetime_index: bool = False) -> Any:
    """Converts `_frame_records` output to the shape the dataset sees.

    Differs from `as_frame`: it doesn't unconditionally convert the index
    to Timestamp. In this family the index is a date in one dataset
    (upgrades_downgrades), a period label ('0q') in another, and a
    sequence number in a third -- `pd.Timestamp('0')` would blow up on
    the third.
    """
    import pandas as pd

    if not records:
        return pd.DataFrame()
    columns = [c for c in records[0] if c != "index"]
    raw_index = [r["index"] for r in records]
    if datetime_index:
        index = pd.Index([pd.Timestamp(v) for v in raw_index], dtype=object)
    else:
        index = pd.Index(raw_index, dtype=object)
    data = {c: [_revive(r.get(c)) for r in records] for c in columns}
    return pd.DataFrame(data, index=index)


def as_funds_data(payload: Any) -> Any:
    """Rebuilds a funds_data fixture into the shape `_collect` outputs."""
    if payload is None:
        return None
    frames = {"fund_operations", "top_holdings", "equity_holdings", "bond_holdings"}
    return {
        key: (as_dataset_frame(value) if key in frames else value)
        for key, value in payload.items()
    }
