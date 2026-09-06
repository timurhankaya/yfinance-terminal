"""Domain dataset'lerinin ortak parcalari (SI S6.4, S6.5, S7.1, S7.6).

VERI KAYNAGI HAM JSON'DUR. yfinance'in `Sector`/`Industry` siniflari ham
yanitin 24 alanini ATIYOR; bunlarin 11'i iki TAM blok (`performance` ve
`performanceOverviewBenchmark`) ve HICBIR property ile erisilemiyorlar
(SI S4.3). `Domain._fetch` zaten `YfData`'nin ince bir sarmalayicisidir;
ham JSON'a inmek yfinance'ten CIKMAK degil, ayni HTTP / proxy / cookie /
curl_cffi katmanini kullanmaktir.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.logging_setup import get_logger

log = get_logger(__name__)

_QUERY = "https://query1.finance.yahoo.com/v1/finance"

# --- tablo adlari ----------------------------------------------------------

DOMAINS_TABLE = "domains"
SYMBOLS_TABLE = "symbols"
METRICS_TABLE = "domain_metrics"
TOP_COMPANIES_TABLE = "domain_top_companies"
TOP_FUNDS_TABLE = "domain_top_funds"
TOP_MOVERS_TABLE = "domain_top_movers"
REPORTS_TABLE = "research_reports"
REPORT_LINKS_TABLE = "domain_report_links"

# --- anahtar evreni --------------------------------------------------------

# Kendi sabitimiz. Yahoo'da "sektorleri listele" ucu YOKTUR; 11/11 canli
# dogrulandi. `test_domain_key_source.py` bu kumenin
# `SECTOR_INDUSTY_MAPPING_LC`'nin ust duzey anahtarlariyla ayni oldugunu
# surer -- kutuphane degisirse sessizce sapmayiz.
#
# ENDUSTRI anahtarlari BURADA YOKTUR ve kutuphane sabitinden HIC IMPORT
# EDILMEZ: `SECTOR_INDUSTY_MAPPING_LC`'nin 145 endustri anahtarinin 32'si
# canli API'de 404 veriyor (const.py:313-318 em-dash ve `&` karakterine
# dokunmuyor: `software—application`, `oil-gas-e&p`). Evren YALNIZ sektor
# yanitinin `industries[].key` alanindan kesfedilir (SI S6.5).
SECTOR_KEYS: tuple[str, ...] = (
    "basic-materials",
    "communication-services",
    "consumer-cyclical",
    "consumer-defensive",
    "energy",
    "financial-services",
    "healthcare",
    "industrials",
    "real-estate",
    "technology",
    "utilities",
)

# --- as-of gunu ------------------------------------------------------------

# 6/6 domain sembolunun timezone'u America/New_York, para birimi USD,
# benchmark "S&P 500" -- bu bir ABD piyasa toplamidir. UTC gunu
# kullanilsaydi 23:30 ve 00:30 kosulari AYNI islem gunu icin IKI satir
# uretirdi ve PK bunu ayirt edemezdi (SI S7.7).
MARKET_TZ = ZoneInfo("America/New_York")


def as_of_day(fetched_at: datetime) -> date:
    moment = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return moment.astimezone(MARKET_TZ).date()


# --- sarmalayici acma ------------------------------------------------------


def unwrap(value: Any) -> Any:
    """Yahoo'nun {"raw":..., "fmt":...} sarmalayicisini acar.

    AYNI ALAN IKI BLOKTA IKI FARKLI BICIMDE gelebiliyor:
    `topCompanies[].targetPrice` SARMALI, `researchReports[].targetPrice`
    CIPLAK float, ve 104 raporun 17'sinde anahtar HIC YOK (SI S4.4). Iki
    ayri ayristirici yazilsaydi biri sessizce None yazardi.
    """
    if isinstance(value, Mapping):
        return value.get("raw")
    return value


def _text(value: Any, max_len: int | None) -> str | None:
    """Metin alani; BOS DIZE NULL'a cevrilir (AH'nin priceTargetAction kurali)."""
    text = nz.to_str(unwrap(value), max_len=max_len)
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def text_of(row: Mapping[str, Any], key: str, max_len: int | None = None) -> str | None:
    return _text(row.get(key), max_len)


def dec_of(row: Mapping[str, Any], key: str) -> Any:
    """DECIMAL(28,12) alani. Eksik anahtar -> None -> NULL."""
    return nz.to_decimal(unwrap(row.get(key)))


def int_of(row: Mapping[str, Any], key: str) -> int | None:
    return nz.to_int(unwrap(row.get(key)))


def big_of(row: Mapping[str, Any], key: str) -> Any:
    from yfin.models.kinds import KINDS

    return KINDS["big"].convert(unwrap(row.get(key)))


def ubig_of(row: Mapping[str, Any], key: str) -> Any:
    from yfin.models.kinds import KINDS

    return KINDS["ubig"].convert(unwrap(row.get(key)))


# --- eslenmis anahtar kumeleri (SI S7.6) -----------------------------------

# `overview` IKI VARYANT tanir: sektorde 7 anahtar (`industriesCount`
# DAHIL), endustride 6 -- anahtar ham JSON'da HIC YOKTUR (145/145 olculdu).
# Tek kume kullanilsaydi her endustri "eksik anahtar" ya da her sektor
# "fazla anahtar" uyarisi uretirdi.
_MAPPED_OVERVIEW_KEYS: dict[str, frozenset[str]] = {
    "sector": frozenset(
        {
            "companiesCount",
            "marketCap",
            "messageBoardId",
            "description",
            "industriesCount",
            "employeeCount",
            "marketWeight",
        }
    ),
    "industry": frozenset(
        {
            "companiesCount",
            "marketCap",
            "messageBoardId",
            "description",
            "employeeCount",
            "marketWeight",
        }
    ),
}

_MAPPED_PERFORMANCE_KEYS = frozenset(
    {
        "ytdChangePercent",
        "regMarketChangePercent",
        "oneYearChangePercent",
        "threeYearChangePercent",
        "fiveYearChangePercent",
    }
)
_MAPPED_BENCHMARK_KEYS = _MAPPED_PERFORMANCE_KEYS | {"name"}

_MAPPED_COMPANY_KEYS = frozenset(
    {
        "symbol",
        "name",
        "rating",
        "marketWeight",
        "marketCap",
        "targetPrice",
        "lastPrice",
        "ytdReturn",
        "regMarketChangePercent",
    }
)
_MAPPED_FUND_KEYS = frozenset(
    {"symbol", "name", "netAssets", "expenseRatio", "lastPrice", "ytdReturn"}
)
_MAPPED_MOVER_KEYS = frozenset(
    {"symbol", "name", "ytdReturn", "lastPrice", "targetPrice", "growthEstimate"}
)
_MAPPED_REPORT_KEYS = frozenset(
    {
        "id",
        "provider",
        "reportType",
        "reportDate",
        "reportTitle",
        "headHtml",
        "targetPrice",
        "targetPriceStatus",
        "investmentRating",
    }
)
_MAPPED_INDUSTRY_ROW_KEYS = frozenset(
    {"key", "name", "symbol", "marketWeight", "ytdReturn", "regMarketChangePercent"}
)
# Yanitin top-level anahtarlari; hepsi ya tipli kolona ya `raw_json`a gider.
_MAPPED_TOP_LEVEL_KEYS = frozenset(
    {
        "key",
        "name",
        "symbol",
        "sectorKey",
        "sectorName",
        "overview",
        "performance",
        "performanceOverviewBenchmark",
        "topCompanies",
        "topETFs",
        "topMutualFunds",
        "topPerformingCompanies",
        "topGrowthCompanies",
        "industries",
        "researchReports",
    }
)

MAPPED_KEYS: dict[str, frozenset[str]] = {
    "performance": _MAPPED_PERFORMANCE_KEYS,
    "performanceOverviewBenchmark": _MAPPED_BENCHMARK_KEYS,
    "topCompanies": _MAPPED_COMPANY_KEYS,
    "topETFs": _MAPPED_FUND_KEYS,
    "topMutualFunds": _MAPPED_FUND_KEYS,
    "topPerformingCompanies": _MAPPED_MOVER_KEYS,
    "topGrowthCompanies": _MAPPED_MOVER_KEYS,
    "researchReports": _MAPPED_REPORT_KEYS,
    "industries": _MAPPED_INDUSTRY_ROW_KEYS,
    "topLevel": _MAPPED_TOP_LEVEL_KEYS,
}


def warn_unmapped(
    dataset: str,
    key: str,
    block: str,
    payload: Any,
    known: frozenset[str],
) -> list[str]:
    """Bilinen kume disindaki anahtarlari WARNING ile loglar.

    Veri KAYBI olmaz (`raw_json`'da durur ya da liste bloklari %100 tipli
    kolona gider); log tipli kolona TERFI sinyalidir
    (`market/status.py`'deki `_MAPPED_SUMMARY_KEYS` deseni).
    """
    rows = payload if isinstance(payload, list) else [payload]
    extra: dict[str, None] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for name in row:
            if name not in known:
                extra[str(name)] = None
    if extra:
        log.warning(
            "unmapped keys", dataset=dataset, domain_key=key, block=block, keys=sorted(extra)
        )
    return sorted(extra)


# --- cekim -----------------------------------------------------------------


def fetch_domain(key: str, domain_type: str, region: str) -> dict[str, Any]:
    """Ham sektor / endustri yaniti.

    `call_optional` KULLANILMAZ, `call_yahoo` kullanilir: 404 -> `failed`
    (SI S8.2). AH S8.4'un "404 -> empty" kurali SEMBOL TARAFI icindir --
    orada anahtar KULLANICININ verdigi bir semboldur ve o sembolde o
    modulun olmamasi mesrudur. Burada anahtar AYNI KOSUDA KENDI
    KESFIMIZDEN gelir; 404 "taksonomi bayat" demektir ve denetimde
    GORUNMESI gerekir.

    `payload["data"]` yoklugu `KeyError('data')` firlatir ->
    `classify_error` -> DATA -> `failed`; bos anahtar bu yoldan yakalanir
    (HTTP durumu YOKTUR, olculdu).
    """
    from yfinance.data import YfData

    path = "sectors" if domain_type == "sector" else "industries"
    params = {
        "formatted": "true",
        "withReturns": "true",
        "lang": "en-US",
        "region": region,
    }
    payload = call_yahoo(
        lambda: YfData().get_raw_json(f"{_QUERY}/{path}/{key}", params=params),
        what=f"{domain_type}:{key}:{region}",
    )
    data = payload["data"]
    if not isinstance(data, dict):  # pragma: no cover - savunma
        raise KeyError("data")
    return data
