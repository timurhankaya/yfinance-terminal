"""Writes the change-event schema, or checks the committed one is current (--check).

The envelope's `row` IS the table row, so the schema is generated from the
models and committed to make every change a reviewable diff. Columns the API
hides are still here: the event is the TABLE, not the resource."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "docs" / "changes" / "schema.json"


def build_document() -> dict[str, Any]:
    """One entry per routed table, from the models and the routing map."""
    import yfin.api.models  # noqa: F401  -- puts the API tables in the metadata
    import yfin.models  # noqa: F401
    from yfin.models.base import Base
    from yfin.storage.changes import BARS_INTERVAL_TABLES, BARS_TIME_COLUMN, ENVELOPE_VERSION
    from yfin.storage.routing import ROUTES
    from yfin.storage.wire import wire_type

    tables: dict[str, Any] = {}
    for name in sorted(ROUTES):
        route = ROUTES[name]
        table = Base.metadata.tables[name]
        entry: dict[str, Any] = {
            "family": route.family.value,
            "topic": f"yfin.changes.{route.family.value}",
            "partition_key": route.partition_column,
            "key_columns": [c.name for c in table.primary_key],
            "columns": {c.name: wire_type(c) for c in table.columns},
        }
        # Only the bars tables can arrive as a span instead of rows, and a
        # consumer needs to know which key column that span is measured
        # over before it can re-read one.
        if name in BARS_TIME_COLUMN:
            entry["range"] = {
                "ts_column": BARS_TIME_COLUMN[name],
                "has_bar_interval": name in BARS_INTERVAL_TABLES,
            }
        tables[name] = entry

    return {"envelope_version": ENVELOPE_VERSION, "tables": tables}


def render(document: dict[str, Any]) -> str:
    # Sorted keys and a trailing newline so the diff shows what changed in
    # the schema, not how the serialiser felt that day.
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; fail if the committed document is out of date.",
    )
    args = parser.parse_args(argv)

    current = render(build_document())
    relative = TARGET.relative_to(REPO_ROOT)

    if not args.check:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(current, encoding="utf-8")
        print(f"wrote {relative}")
        return 0

    if not TARGET.exists():
        print(f"{relative} is missing; run this script", file=sys.stderr)
        return 1

    if TARGET.read_text(encoding="utf-8") == current:
        print(f"{relative} is up to date")
        return 0

    print(
        f"{relative} is out of date. What consumers receive changed; "
        "regenerate it and review the diff:\n"
        "    python scripts/dump_change_schema.py",
        file=sys.stderr,
    )
    generated = TARGET.with_suffix(".json.generated")
    generated.write_text(current, encoding="utf-8")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "--no-pager", "diff", "--no-index", "--", str(TARGET), str(generated)],
        check=False,
    )
    generated.unlink(missing_ok=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
