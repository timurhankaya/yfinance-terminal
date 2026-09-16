"""`docs/changes/schema.json` describes what consumers receive: the envelope's `row` IS the
table row, so committing and diffing it makes a column added anywhere a reviewable change
to the event stream. The committed file must match the models."""

from __future__ import annotations

import json
from typing import Any

import pytest
from scripts.dump_change_schema import TARGET, build_document, render

from yfin.storage.changes import ENVELOPE_VERSION
from yfin.storage.routing import ROUTES


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return build_document()


def test_the_committed_document_is_current() -> None:
    """The lock itself. CI runs `--check`; this is the same comparison, so
    a stale file fails in the fast test run too."""
    assert TARGET.exists(), "run: python scripts/dump_change_schema.py"
    assert TARGET.read_text(encoding="utf-8") == render(build_document()), (
        "docs/changes/schema.json is stale; regenerate it and review the diff:\n"
        "    python scripts/dump_change_schema.py"
    )


def test_it_describes_every_routed_table(document: dict[str, Any]) -> None:
    """A routed table missing here is a topic a consumer cannot read."""
    assert set(document["tables"]) == set(ROUTES)


def test_it_carries_the_envelope_version(document: dict[str, Any]) -> None:
    """Removing or renaming a field bumps it, so a consumer can refuse a
    version it does not understand."""
    assert document["envelope_version"] == ENVELOPE_VERSION


def test_every_table_names_its_topic_and_key(document: dict[str, Any]) -> None:
    for name, entry in document["tables"].items():
        route = ROUTES[name]
        assert entry["topic"] == f"yfin.changes.{route.family.value}"
        assert entry["partition_key"] == route.partition_column
        assert entry["partition_key"] in entry["key_columns"]


def test_every_column_has_a_wire_type(document: dict[str, Any]) -> None:
    known = {
        "string",
        "integer",
        "boolean",
        "string (decimal)",
        "string (date-time)",
        "string (date)",
    }
    for name, entry in document["tables"].items():
        assert entry["columns"], name
        for column, wire in entry["columns"].items():
            assert wire in known, f"{name}.{column} -> {wire}"


def test_hidden_api_columns_are_still_published(document: dict[str, Any]) -> None:
    """The event is the TABLE, not the resource. `screens` hides its query
    definition from the REST surface; a consumer mirroring the table still
    has to be told when it changes."""
    from yfin.api.storage.catalog import CATALOG

    hidden = {
        (entry.table.name, column)
        for entry in CATALOG.values()
        for column in entry.exposure.hidden
    }
    assert hidden, "no exposure hides a column; this test has stopped proving anything"
    for table, column in hidden:
        if table in document["tables"]:
            assert column in document["tables"][table]["columns"]


def test_only_bars_tables_declare_a_range(document: dict[str, Any]) -> None:
    """Coalescing is what bars do; a `range` on anything else would hide a
    row the consumer could have applied directly."""
    from yfin.core.families import DataFamily

    for name, entry in document["tables"].items():
        has_range = "range" in entry
        assert has_range == (ROUTES[name].family is DataFamily.BARS), name


def test_a_range_names_the_column_its_span_is_over(document: dict[str, Any]) -> None:
    """Without it a consumer cannot re-read the span it was just told
    about."""
    for name, entry in document["tables"].items():
        if "range" not in entry:
            continue
        ts_column = entry["range"]["ts_column"]
        assert ts_column in entry["columns"], name
        assert ts_column in entry["key_columns"], name


def test_the_document_is_deterministic() -> None:
    """Sorted keys, so the diff shows what changed in the schema rather
    than how the serialiser felt that day."""
    assert render(build_document()) == render(build_document())
    assert json.loads(render(build_document()))["tables"]
