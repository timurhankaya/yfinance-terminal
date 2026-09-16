"""What publishing change events costs the write path.

Syncs the SAME symbol three times against a WARM database (off, on, off); the
third pass bounds run-to-run variance. Not `--full-refresh`: that measures a
run nobody makes. Usage: measure_change_cost.py --symbol AAPL (needs network + DB)."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from yfin.core.config import get_settings
from yfin.storage.db import create_db_engine

#: The console script, next to the interpreter running this. There is no
#: `yfin.__main__`, so `-m yfin` does not work.
YFIN = str(Path(sys.executable).with_name("yfin"))


def _set(key: str, value: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [YFIN, "config", "set", key, value],
        check=True,
        capture_output=True,
    )


def _sync(symbol: str) -> float:
    started = time.perf_counter()
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [YFIN, "sync", "--symbols", symbol],
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    # Exit 2 is "partial", which a live sync often is (a dataset legitimately
    # empty for this symbol). Only a total failure invalidates the timing.
    if result.returncode not in (0, 2):
        print(result.stdout[-2000:], file=sys.stderr)
        raise SystemExit(f"sync failed with {result.returncode}")
    return elapsed


def _outbox_snapshot(engine: Any) -> list[tuple[str, str, int]]:
    """Events queued so far, by table and op."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT payload::json ->> 'table' AS t, "
                    "       payload::json ->> 'op' AS op, count(*) "
                    "  FROM pipeline_outbox GROUP BY t, op ORDER BY count(*) DESC, t"
                )
            ).all()
        )


def _clear_outbox(engine: Any) -> None:
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM pipeline_outbox"))
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="AAPL")
    args = parser.parse_args(argv)

    engine = create_db_engine(get_settings())

    print(f"symbol: {args.symbol}")
    print("protocol: warm database, three passes -- off, on, off\n")

    _set("yf_changes_enabled", "false")
    off_first = _sync(args.symbol)
    print(f"pass 1  collector OFF : {off_first:7.2f}s")

    _clear_outbox(engine)
    _set("yf_changes_enabled", "true")
    on = _sync(args.symbol)
    print(f"pass 2  collector ON  : {on:7.2f}s")

    events = _outbox_snapshot(engine)

    _set("yf_changes_enabled", "false")
    off_second = _sync(args.symbol)
    print(f"pass 3  collector OFF : {off_second:7.2f}s")

    baseline = (off_first + off_second) / 2
    spread = abs(off_first - off_second) / baseline * 100 if baseline else 0.0
    overhead = (on - baseline) / baseline * 100 if baseline else 0.0
    print()
    print(f"off baseline (mean of 1 and 3): {baseline:7.2f}s")
    print(f"run-to-run spread of the two off passes: {spread:5.1f}%")
    print(f"collector overhead against that baseline: {overhead:+5.1f}%")
    if abs(overhead) <= spread:
        print("-> smaller than the spread; report as noise, not as a cost")

    print()
    print("events queued by pass 2, by table and op:")
    if not events:
        print("  (none -- an unchanged sync publishes nothing, which is the point)")
    for table, op, count in events:
        print(f"  {table:28s} {op:8s} {count:>6}")
    print(f"  total: {sum(c for _, _, c in events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
