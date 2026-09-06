"""Verifies the field map and the model columns do not diverge."""

from __future__ import annotations

from yfin.models import Base
from yfin.models.fields import (
    FAST_INFO_FIELDS,
    HISTORY_METADATA_FIELDS,
    INFO_FIELDS,
    Field,
)

CASES = (
    ("ticker_info", INFO_FIELDS),
    ("ticker_info_history", INFO_FIELDS),
    ("ticker_fast_info", FAST_INFO_FIELDS),
    ("ticker_fast_info_history", FAST_INFO_FIELDS),
    ("history_metadata", HISTORY_METADATA_FIELDS),
)


def test_every_field_has_a_column() -> None:
    for table_name, fields in CASES:
        columns = set(Base.metadata.tables[table_name].c.keys())
        for field in fields:
            assert field.column in columns, f"{table_name}.{field.column} is missing"


def test_no_duplicate_sources_or_columns() -> None:
    for _, fields in CASES:
        sources = [f.source for f in fields]
        columns = [f.column for f in fields]
        assert len(set(sources)) == len(sources)
        assert len(set(columns)) == len(columns)


def test_fast_info_has_exactly_20_keys() -> None:
    """The source hardcodes 20 keys (quote.py:_public_keys)."""
    assert len(FAST_INFO_FIELDS) == 20



def test_epoch_fields_use_epoch_kind() -> None:
    from yfin.core import normalize as nz

    for field in INFO_FIELDS:
        if field.source in nz.EPOCH_MS_FIELDS:
            assert field.kind == "epoch_ms", field.source
        elif field.source in nz.EPOCH_SEC_FIELDS:
            assert field.kind == "epoch_s", field.source


def test_not_epoch_fields_are_not_datetime() -> None:
    from yfin.core import normalize as nz

    by_source: dict[str, Field] = {f.source: f for f in INFO_FIELDS}
    for source in nz.NOT_EPOCH_FIELDS:
        field = by_source.get(source)
        if field is not None:
            assert field.kind not in ("epoch_s", "epoch_ms", "dt"), source
