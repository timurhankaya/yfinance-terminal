"""MySQL'in utf8mb4_0900_ai_ci varsayilaninin kaldirilmasiyla ortaya
cikan davranis farklari (PG S2.5). Veritabanina DOKUNMAZ.

MySQL'de tablo varsayilani BUYUK/KUCUK HARF DUYARSIZDI ve iki yerde
gercek semantik tasiyordu. PostgreSQL'de kolonlar COLLATE "C"dir
(duyarli); duyarsizlik YAZMA ve SORGU yollarina tasindi. Bu testler o
tasimanin yerinde durdugunu sabitler -- aksi halde belirti sessizdir:
`--exchange nms` bir gun bos sonuc doner, ya da ayni proxy iki kez
eklenir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load_seed_proxies() -> Any:
    """`scripts/` bir paket DEGILDIR; dosyadan yuklenir."""
    path = _ROOT / "scripts" / "seed_proxies.py"
    spec = importlib.util.spec_from_file_location("_seed_proxies", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_seed_proxies"] = module
    spec.loader.exec_module(module)
    return module


class TestSymbolFieldsAreUppercased:
    """`--exchange nms` ile `--exchange NMS` ayni sonucu vermeli.

    MySQL'de kolonlar ai_ci oldugu icin karsilastirma zaten duyarsizdi ve
    `_filtered_symbols` bunu ACIKCA gerekce gostererek `func.upper`
    KULLANMIYORDU. PostgreSQL'de o gerekce cokuyor.
    """

    def test_normalize_upper_cases_exchange_and_quote_type(self) -> None:
        from yfin.datasets.symbols import SymbolsDataset, SymbolsPayload

        payload = SymbolsPayload(
            fast_info={"quoteType": "equity", "exchange": "nms", "currency": "usd"},
            metadata={},
            fetched_at=None,
        )
        result = SymbolsDataset().normalize(payload, "AAPL")
        row = result.writes[0].rows[0]
        assert row["quote_type"] == "EQUITY"
        assert row["exchange"] == "NMS"

    def test_none_stays_none(self) -> None:
        """`yfin symbols add` ile eklenen sembolde bu alanlar ILK SYNC'E
        KADAR NULL'dur; .upper() None'i patlatmamali."""
        from yfin.datasets.symbols import SymbolsDataset, SymbolsPayload

        payload = SymbolsPayload(fast_info={}, metadata={}, fetched_at=None)
        row = SymbolsDataset().normalize(payload, "AAPL").writes[0].rows[0]
        assert row["quote_type"] is None
        assert row["exchange"] is None


class TestProxyHostIsLowercased:
    """Hostname'ler buyuk/kucuk harf duyarsizdir (RFC 4343).

    MySQL bunu `ascii_general_ci` ile SEMADA sagliyordu ve
    `uq_proxies_endpoint (scheme, host, port, username)` buna dayaniyordu.
    PostgreSQL'de duyarsizlik yazma yolunda saglanir; aksi halde ayni
    proxy farkli harf bicimleriyle IKI KEZ eklenirdi.
    """

    def test_seed_line_lowercases_host(self) -> None:
        from yfin.models import ProxyScheme

        seed = _load_seed_proxies()
        left = seed.parse_line("HOST.Example.COM:8080", ProxyScheme.HTTP)
        right = seed.parse_line("host.example.com:8080", ProxyScheme.HTTP)
        assert left is not None and right is not None
        assert left.host == "host.example.com"
        assert left.host == right.host

    def test_dsn_path_already_lowercases(self) -> None:
        """`urlsplit(...).hostname` hostname'i ZATEN kucuk harfe indirir;
        burada yapacak is yoktur ama regresyon olarak sabitlenir --
        ileride elle ayristirmaya gecilirse bu test duser."""
        from yfin.proxy.dsn import parse_dsn

        dsn = "http://u:pw" + "@" + "HOST.Example.COM:8080"
        assert parse_dsn(dsn).host == "host.example.com"


@pytest.mark.parametrize("given", ["nms", "NMS", "Nms"])
def test_cli_filter_input_is_normalized(given: str) -> None:
    """Filtre girdisi de `.upper()` ile normalize edilir; yazma yolu
    buyuk harfe cevirdigi icin iki taraf ayni bicimde bulusur ve
    `ix_symbols_exchange` indeksi kullanilabilir kalir (func.upper
    kolonu sarmalasaydi kullanilamazdi)."""
    from yfin.cli import _normalize_filter_values

    assert _normalize_filter_values([given]) == ["NMS"]
