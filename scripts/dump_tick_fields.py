"""Writes the tick field table for the browser, or checks the committed copy.

`stream/publish.py` owns the wire shape of a tick. The page has to know
the same table to type it, and a TypeScript interface written by hand
next to a Python tuple is two sources of truth that agree until the day
they do not -- a renamed key would reach production as an undefined
price, silently, on a chart that still drew.

So the table is generated here and committed, the same arrangement
`openapi.json` has: CI runs `--check`, and a key that moved shows up as a
failing build with a diff rather than as a blank field.

Usage:
    python scripts/dump_tick_fields.py            # write the JSON
    python scripts/dump_tick_fields.py --check    # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "web" / "src" / "live" / "tick-fields.json"


def build_table() -> dict[str, Any]:
    from yfin.stream.publish import CHANNEL_PREFIX, TICK_FIELDS

    return {
        "channelPrefix": CHANNEL_PREFIX,
        "fields": [
            {"key": key, "column": column, "kind": kind, "required": required}
            for key, column, kind, required in TICK_FIELDS
        ],
    }


def render(table: dict[str, Any]) -> str:
    return json.dumps(table, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; fail if the committed table is out of date.",
    )
    args = parser.parse_args(argv)

    current = render(build_table())

    if not args.check:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(current, encoding="utf-8")
        print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
        return 0

    if not TARGET.exists():
        print(
            f"{TARGET.relative_to(REPO_ROOT)} is missing; run this script",
            file=sys.stderr,
        )
        return 1

    if TARGET.read_text(encoding="utf-8") == current:
        print("tick-fields.json is up to date")
        return 0

    print(
        "tick-fields.json is out of date. The tick wire shape changed; "
        "regenerate it and review the diff:\n"
        "    python scripts/dump_tick_fields.py",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
