"""KIRILGANLIK CITI: endustri anahtarlarinin kaynagi (SI S9.2, S14/0).

Bu dosya spec'in VARLIK NEDENIDIR ve hicbir uygulama kodundan once
yazilmistir. Kanitladigi sey su: `SECTOR_INDUSTY_MAPPING_LC`'den turetilen
endustri anahtarlari CANLI API ILE UYUSMUYOR -- `const.py:313-318`
`k.lower().replace('& ','')...` uyguluyor ama EM-DASH ve ampersand
karakterine dokunmuyor.

Kutuphane bunu bir gun duzeltirse test PATLAR ve o gun `SECTOR_KEYS`in
yaninda endustri anahtarlarini da sabitten almak yeniden tartisilabilir.
Duzeltilmeden bir gelecek degisiklik sabitten seed ederse 32 endustri
sessizce `empty` yazilir, DB'de 145 yerine 113 endustri olur ve denetim
"hata yok" derdi.
"""

from __future__ import annotations

import ast
import pathlib

from helpers import domain_data, domain_fixture_keys
from yfin.datasets.domain.common import SECTOR_KEYS

EM_DASH = "—"
AMPERSAND = "&"


def _library_mapping() -> dict[str, list[str]]:
    from yfinance.const import SECTOR_INDUSTY_MAPPING_LC

    return {str(k): [str(v) for v in vals] for k, vals in SECTOR_INDUSTY_MAPPING_LC.items()}


def test_sector_keys_match_the_library_top_level_keys() -> None:
    """SEKTOR anahtarlari icin sabit GUVENILIR: 11/11 canlida dogrulandi."""
    assert set(SECTOR_KEYS) == set(_library_mapping())
    assert len(SECTOR_KEYS) == 11


def test_industry_keys_from_the_library_differ_from_the_live_response() -> None:
    """Fixture'lardan kesfedilen anahtar kumesi ile kutuphaneninki AYNI DEGIL.

    Fark tam olarak em-dash / ampersand tasiyan anahtarlardadir.
    """
    library = _library_mapping()
    mismatched: dict[str, tuple[set[str], set[str]]] = {}
    for key, region in domain_fixture_keys("sector"):
        if region != "US":
            continue
        discovered = {
            row["key"] for row in domain_data("sector", key)["industries"] if "key" in row
        }
        expected = set(library[key])
        if discovered != expected:
            mismatched[key] = (discovered - expected, expected - discovered)

    assert mismatched, (
        "kutuphane sabiti ile canli anahtarlar artik AYNI. "
        "SI S4.1'in bulgusu gecersizlesmis olabilir: endustri anahtar "
        "kaynagi kararini yeniden degerlendirin."
    )
    # Kaybolan her anahtar em-dash ya da ampersand tasiyor; canli karsiligi
    # duz tire kullaniyor.
    for _key, (only_live, only_library) in mismatched.items():
        assert only_library, "kutuphanede fazladan anahtar bekleniyordu"
        for name in only_library:
            assert EM_DASH in name or AMPERSAND in name, name
        for name in only_live:
            assert EM_DASH not in name and AMPERSAND not in name, name


def test_utilities_is_the_core_evidence() -> None:
    """`utilities`in ALTI anahtarinin ALTISI da sabitten farkli."""
    library = set(_library_mapping()["utilities"])
    live = {row["key"] for row in domain_data("sector", "utilities")["industries"] if "key" in row}
    assert len(live) == 6
    assert live.isdisjoint(library)


def test_industry_keys_are_never_imported_from_the_library() -> None:
    """Kutuphanenin endustri haritasi HICBIR uygulama modulunde IMPORT EDILMEZ.

    Kontrol AST uzerindedir, metin aramasi degil: `common.py` bu sabitin
    ADINI bir YORUMDA anip neden kullanilmadigini anlatiyor ve bu istenen
    seydir. Yasak olan sey ondan VERI ALMAK.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "yfin"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "yfinance.const"
            ):
                offenders.append(str(path.relative_to(root)))
            elif isinstance(node, ast.Import):
                offenders.extend(
                    str(path.relative_to(root))
                    for alias in node.names
                    if alias.name.startswith("yfinance.const")
                )
    assert offenders == []
