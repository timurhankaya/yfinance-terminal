"""Parsing rules for the string forms configuration and the CLI use.

Imports nothing, on purpose. `api/core/config.py` needs the comma rule and
must stay importable without dragging the ORM or pandas in behind it --
that is what keeps `yfin --help` at 561 modules instead of 1529.
"""

from __future__ import annotations


def comma_list(value: str | None, *, upper: bool = False) -> list[str]:
    """A comma-separated setting, split into its entries.

    Empty entries are dropped, which is the part worth having in one
    place: `US,,GB` and a trailing comma both come from hand-edited `.env`
    files, and an empty string reaching a region loop is a request for
    `""` that fails somewhere far from the typo. Eight call sites spelled
    this out separately -- five of them also upper-casing, which is why
    that is a flag here rather than a second function.
    """
    if not value:
        return []
    parts = (part.strip() for part in value.split(","))
    return [part.upper() if upper else part for part in parts if part]
