"""`ast` fence: no `get_settings()` call in a module body. Through the `cli.py ->
yfin.datasets -> ...` import chain it would force even `yfin --help` to build Settings and
connect to the database, break recovery commands while the DB is down, and defeat test
isolation (pytest imports before fixtures run)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "yfin"


def _module_level_calls(tree: ast.Module) -> list[ast.Call]:
    """Only the MODULE BODY. Function/method bodies are out of scope:
    calling `get_settings()` there is a correct and common pattern."""
    calls: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        calls.extend(n for n in ast.walk(node) if isinstance(n, ast.Call))
    return calls


def test_no_get_settings_call_in_a_module_body() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _module_level_calls(tree):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "get_settings":
                offenders.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert not offenders, "get_settings() in a module body: " + ", ".join(offenders)
