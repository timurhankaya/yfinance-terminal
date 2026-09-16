"""What `yfin --help` may import. Command modules import the dataset registry and the model
package inside command bodies; one module-level `from yfin.models import ...` would put the
full import cost back on every invocation and nothing else would notice."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _import_probe(module: str) -> set[str]:
    """Imports `module` in a fresh interpreter and reports what came with it. A subprocess,
    because this process has already imported half the package."""
    code = textwrap.dedent(f"""
        import sys
        import {module}
        print("\\n".join(sorted(sys.modules)))
    """)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def test_the_cli_entry_point_pulls_in_neither_the_registry_nor_the_orm() -> None:
    loaded = _import_probe("yfin.cli.app")
    heavy = {name for name in loaded if name.startswith(("yfin.datasets", "yfin.models"))}
    assert not heavy, (
        "importing the CLI entry point loaded the dataset registry or the model "
        f"package; move these imports into the command bodies: {sorted(heavy)}"
    )


def test_the_cli_scope_list_agrees_with_the_scope_enum() -> None:
    """`cli/api._scope_values` derives the scope names from `core.families`
    instead of reading `ApiScope`, because reading it would import the ORM
    while the command decorators are still being evaluated. Both derive from
    the same pair, and this is what keeps them from drifting apart."""
    from yfin.api.models.clients import ApiScope
    from yfin.cli.api import _scope_values

    assert _scope_values() == [scope.value for scope in ApiScope]
