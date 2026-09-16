"""Writes the OpenAPI document, or checks the committed one is current (--check).

Committing the generated file and diffing it in CI turns every contract
change into a reviewable diff."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "openapi.json"


def build_document() -> dict[str, Any]:
    """The document as the running application would serve it.

    Settings are supplied explicitly: the document must not depend on whose
    machine generated it."""
    from yfin.api.app import create_app
    from yfin.api.core.config import ApiSettings

    app = create_app(
        ApiSettings(
            # A developer's own .env must not be able to change -- or
            # break -- what this script checks.
            _env_file=None,
            jwt_signing_key="x" * 32,
            jwt_kid="k1",
            jwt_issuer="yfin-api",
            jwt_audience="yfin-api",
            docs_enabled=True,
            cors_origins="",
            trusted_proxies="",
            # Pinned like the rest: `servers` is part of the contract, so
            # the committed document must not pick up whatever host the
            # generating machine happens to be configured for.
            public_base_url="",
            # The UI is not part of the /v1 contract this script checks;
            # keep it off so the document never depends on the frontend
            # build being present.
            ui_enabled=False,
        )
    )
    return app.openapi()


def render(document: dict[str, Any]) -> str:
    # Sorted keys and a trailing newline so the diff shows what changed in
    # the contract, not how the serialiser felt that day.
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

    if not args.check:
        TARGET.write_text(current, encoding="utf-8")
        print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
        return 0

    if not TARGET.exists():
        print(f"{TARGET.relative_to(REPO_ROOT)} is missing; run this script", file=sys.stderr)
        return 1

    committed = TARGET.read_text(encoding="utf-8")
    if committed == current:
        print("openapi.json is up to date")
        return 0

    print(
        "openapi.json is out of date. The contract changed; regenerate it and "
        "review the diff:\n    python scripts/dump_openapi.py",
        file=sys.stderr,
    )
    # Show what moved, so CI output answers the question on its own.
    TARGET.with_suffix(".json.generated").write_text(current, encoding="utf-8")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "git",
            "--no-pager",
            "diff",
            "--no-index",
            "--",
            str(TARGET),
            str(TARGET.with_suffix(".json.generated")),
        ],
        check=False,
    )
    TARGET.with_suffix(".json.generated").unlink(missing_ok=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
