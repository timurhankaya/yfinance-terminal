"""Every dataset module must actually reach a registry.

Registration happens as an import side effect, so the one central list
left in the architecture is `datasets/__init__.py`. Leaving a module out
of it is silent: the dataset simply never exists, `yfin sync` runs
without it, and no test fails because no test knows it should be there.
This walks the package on disk instead and compares.
"""

from __future__ import annotations

import ast
import functools
import json
import pathlib
import pkgutil
import subprocess
import sys

import yfin.datasets
from yfin.datasets.registry import DOMAIN_DATASETS, MARKET_DATASETS, SYMBOL_DATASETS

PACKAGE = pathlib.Path(yfin.datasets.__file__).parent
REGISTER_CALLS = {"register", "register_market", "register_domain"}


def _modules_that_register() -> set[str]:
    """Modules whose source contains a module-level registration call."""
    found: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name in REGISTER_CALLS:
                rel = path.relative_to(PACKAGE).with_suffix("")
                found.add("yfin.datasets." + ".".join(rel.parts))
                break
    return found


def test_every_registering_module_is_imported_by_the_package() -> None:
    """A module that registers but is never imported is dead weight the
    CLI cannot see. This is the failure `datasets/__init__.py` invites."""
    registered = _modules_that_register()
    missing = [name for name in sorted(registered) if name not in _imported_modules()]
    assert not missing, "these modules register a dataset but are never imported: " + ", ".join(
        missing
    )


@functools.cache
def _imported_modules() -> frozenset[str]:
    """Every yfin.datasets module reachable from importing ONLY the package.

    This runs in a subprocess on purpose. Reading this process's
    `sys.modules` would count modules some other test imported directly,
    so a module missing from `datasets/__init__.py` would still look
    present and the guard would pass while the CLI saw nothing.
    """
    code = (
        "import sys, json; import yfin.datasets; "
        "print(json.dumps([m for m in sys.modules if m.startswith('yfin.datasets')]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    return frozenset(json.loads(out))


def test_no_registering_subpackage_is_left_out_of_the_import_list() -> None:
    """Adding a subpackage without listing it in `__init__` registers
    nothing at all -- every dataset inside it disappears at once.

    Support modules that register nothing (`meta`, `payloads`) are not
    required here; only the ones that carry datasets."""
    imported = _imported_modules()
    registering = _modules_that_register()
    for info in pkgutil.iter_modules([str(PACKAGE)]):
        if info.name.startswith("_"):
            continue
        prefix = f"yfin.datasets.{info.name}"
        if not any(name == prefix or name.startswith(prefix + ".") for name in registering):
            continue
        assert prefix in imported, info.name


def test_the_three_registries_share_no_names() -> None:
    """`yfin datasets` prints all three and the CLI resolves by name; a
    collision would make which one you get depend on lookup order."""
    # `all` is excluded: it is a keyword every registry understands as
    # "everything in THIS one", not a name that could resolve to a
    # different dataset depending on which registry was asked first.
    keyword = {"all"}
    symbol = set(SYMBOL_DATASETS.user_visible_names()) - keyword
    market = set(MARKET_DATASETS.user_visible_names()) - keyword
    domain = set(DOMAIN_DATASETS.user_visible_names()) - keyword
    assert not symbol & market
    assert not symbol & domain
    assert not market & domain
