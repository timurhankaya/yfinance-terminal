"""`datasets/` must not depend on SQLAlchemy; the layer talks to storage through a protocol."""

from __future__ import annotations

import ast
import pathlib

from yfin.storage.contracts import VariantState
from yfin.storage.variants import ScreenVariantState

DATASETS = pathlib.Path(__file__).resolve().parents[2] / "src" / "yfin" / "datasets"

#: Storage modules a dataset may not reach for: the SQL the writer emits, where a row's
#: event is routed, and how one is captured. `contracts` (the protocol) and the `models.*`
#: constant modules stay allowed.
FORBIDDEN_MODULES = (
    "yfin.storage.persistence",
    "yfin.storage.routing",
    "yfin.storage.changes",
)


def _imported_roots(path: pathlib.Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Full dotted names, so `yfin.storage.contracts` and
    `yfin.storage.routing` can be told apart."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_no_dataset_module_imports_sqlalchemy() -> None:
    offenders = [
        str(p.relative_to(DATASETS))
        for p in DATASETS.rglob("*.py")
        if "sqlalchemy" in _imported_roots(p)
    ]
    assert not offenders, "sqlalchemy imported in datasets/: " + ", ".join(offenders)


def test_no_dataset_module_imports_the_write_machinery() -> None:
    """Three named modules rather than "anything under storage": `storage.contracts` is
    exactly what a dataset is supposed to import."""
    offenders = [
        f"{p.relative_to(DATASETS)}: {module}"
        for p in DATASETS.rglob("*.py")
        for module in sorted(_imported_modules(p) & set(FORBIDDEN_MODULES))
    ]
    assert not offenders, "write machinery imported in datasets/: " + ", ".join(offenders)


def test_the_storage_implementation_satisfies_the_protocol() -> None:
    """A structural protocol is only a contract if something is checked against it; nothing
    else in the codebase would catch a rename."""
    checked: list[VariantState] = [ScreenVariantState(None)]  # type: ignore[arg-type]
    assert all(hasattr(state, "disabled_variants") for state in checked)
