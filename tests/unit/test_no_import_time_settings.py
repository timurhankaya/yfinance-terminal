"""`ast` citi: modul govdesinde `get_settings()` cagrisi YOK (CFG S8.1).

Bu tuzak kod tabaninda IKI KEZ kuruldu (`yf_discovery_enabled`, sonra
`yf_probe_sustainability`). Ucuncusunu bu test onler.

Neden olumcul: `cli.py -> yfin.datasets -> ... -> <modul>` zinciri
yuzunden `yfin --help` bile Settings'i kurmaya zorlanirdi. DB katmani
devredeyken bu, (1) DB'ye HIC dokunmayan komutlarin DB'ye baglanmasi,
(2) DB kapaliyken KURTARMA komutlarinin da calismamasi, (3) test
izolasyonunun kurulamamasi demektir -- pytest once modulleri import
eder, fixture'lar sonra kosar.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "yfin"


def _module_level_calls(tree: ast.Module) -> list[ast.Call]:
    """Yalniz MODUL GOVDESI. Fonksiyon/metot govdeleri kapsam disidir:
    orada `get_settings()` cagirmak dogru ve yaygin desendir."""
    calls: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        calls.extend(n for n in ast.walk(node) if isinstance(n, ast.Call))
    return calls


def test_modul_govdesinde_get_settings_cagrisi_yok() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _module_level_calls(tree):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "get_settings":
                offenders.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert not offenders, "modul govdesinde get_settings(): " + ", ".join(offenders)
