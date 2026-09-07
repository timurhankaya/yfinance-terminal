"""`datasets/` must not depend on SQLAlchemy.

The claim "the dataset layer talks to storage through a protocol" is only
worth something if it is enforced. It was not: `market/base.py` took a
`sqlalchemy.orm.Session` in the `variants()` contract and `screener.py`
ran its own SELECT, so the boundary held everywhere except the one place
that mattered.
"""

from __future__ import annotations

import ast
import pathlib

from yfin.storage.contracts import VariantState
from yfin.storage.variants import NoVariantState, ScreenVariantState

DATASETS = pathlib.Path(__file__).resolve().parents[2] / "src" / "yfin" / "datasets"

#: Storage modules a dataset may not reach for.
#:
#: `contracts` is the protocol the boundary is stated in, and the eight
#: `models.*` constant modules datasets import carry field and kind names
#: rather than machinery, so both stay allowed. These three are the
#: machinery: the SQL the writer emits, where a row's event is routed, and
#: how one is captured. A dataset that imported any of them would be
#: deciding how it is written, which is the storage layer's decision.
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
    """The protocol is the boundary; the implementation behind it is not.

    Stated as three named modules rather than "anything under storage"
    because `storage.contracts` is exactly what a dataset is supposed to
    import -- the rule has to be able to say which side of the line each
    module is on.
    """
    offenders = [
        f"{p.relative_to(DATASETS)}: {module}"
        for p in DATASETS.rglob("*.py")
        for module in sorted(_imported_modules(p) & set(FORBIDDEN_MODULES))
    ]
    assert not offenders, "write machinery imported in datasets/: " + ", ".join(offenders)


def test_the_storage_implementations_satisfy_the_protocol() -> None:
    """A structural protocol is only a contract if something is checked
    against it; nothing else in the codebase would catch a rename."""
    checked: list[VariantState] = [NoVariantState(), ScreenVariantState(None)]  # type: ignore[arg-type]
    assert all(hasattr(state, "disabled_variants") for state in checked)


def test_no_variant_state_disables_nothing() -> None:
    """Library use and tests have no database; the dataset must then see
    the full screen set rather than an empty one."""
    assert NoVariantState().disabled_variants() == frozenset()
