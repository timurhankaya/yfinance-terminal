"""Parsing rules for the string forms configuration and the CLI use.

Imports nothing, on purpose: `api/core/config.py` needs the comma rule and must
stay importable without dragging the ORM or pandas in behind it."""

from __future__ import annotations


def comma_list(value: str | None, *, upper: bool = False) -> list[str]:
    """A comma-separated setting, split into its entries.

    Empty entries are dropped: `US,,GB` and a trailing comma come from
    hand-edited `.env` files, and an empty region fails far from the typo."""
    if not value:
        return []
    parts = (part.strip() for part in value.split(","))
    return [part.upper() if upper else part for part in parts if part]
