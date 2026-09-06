"""Proof of the source of industry keys.

This file is the reason the spec exists and was written before any
implementation code. What it proves: industry keys derived from
`SECTOR_INDUSTY_MAPPING_LC` do NOT match the live API -- `const.py:313-318`
applies `k.lower().replace('& ','')...` but does not touch the em-dash or
ampersand characters.

If the library ever fixes this, this test breaks, and taking industry keys
from the constant (like `SECTOR_KEYS`) becomes worth reconsidering. Until
it's fixed, a future change that seeds from the constant would silently
write 32 industries as `empty`, leave the DB with 113 industries instead of
145, and an audit would say "no error".
"""

from __future__ import annotations

import ast
import pathlib

from helpers import domain_data, domain_fixture_keys
from yfin.datasets.domain.common import SECTOR_KEYS

EM_DASH = "—"
AMPERSAND = "&"


def _library_mapping() -> dict[str, list[str]]:
    from yfinance.const import SECTOR_INDUSTY_MAPPING_LC

    return {str(k): [str(v) for v in vals] for k, vals in SECTOR_INDUSTY_MAPPING_LC.items()}


def test_sector_keys_match_the_library_top_level_keys() -> None:
    """The constant is reliable for sector keys: verified 11/11 live."""
    assert set(SECTOR_KEYS) == set(_library_mapping())
    assert len(SECTOR_KEYS) == 11


def test_industry_keys_from_the_library_differ_from_the_live_response() -> None:
    """The key set discovered from fixtures does NOT match the library's.

    The difference is exactly in keys carrying an em-dash or ampersand.
    """
    library = _library_mapping()
    mismatched: dict[str, tuple[set[str], set[str]]] = {}
    for key, region in domain_fixture_keys("sector"):
        if region != "US":
            continue
        discovered = {
            row["key"] for row in domain_data("sector", key)["industries"] if "key" in row
        }
        expected = set(library[key])
        if discovered != expected:
            mismatched[key] = (discovered - expected, expected - discovered)

    assert mismatched, (
        "the library constant and the live keys now match. "
        "The earlier finding may be invalidated: reconsider the industry "
        "key source decision."
    )
    # Every missing key carries an em-dash or ampersand; its live
    # counterpart uses a plain hyphen instead.
    for _key, (only_live, only_library) in mismatched.items():
        assert only_library, "expected extra keys in the library"
        for name in only_library:
            assert EM_DASH in name or AMPERSAND in name, name
        for name in only_live:
            assert EM_DASH not in name and AMPERSAND not in name, name


def test_utilities_is_the_core_evidence() -> None:
    """All six of `utilities`' keys differ from the constant."""
    library = set(_library_mapping()["utilities"])
    live = {row["key"] for row in domain_data("sector", "utilities")["industries"] if "key" in row}
    assert len(live) == 6
    assert live.isdisjoint(library)


def test_industry_keys_are_never_imported_from_the_library() -> None:
    """The library's industry map is not imported by any application module.

    The check runs on the AST, not a text search: `common.py` mentions this
    constant's name in a comment to explain why it is not used, which is
    fine. What is forbidden is pulling data from it.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "yfin"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "yfinance.const"
            ):
                offenders.append(str(path.relative_to(root)))
            elif isinstance(node, ast.Import):
                offenders.extend(
                    str(path.relative_to(root))
                    for alias in node.names
                    if alias.name.startswith("yfinance.const")
                )
    assert offenders == []
