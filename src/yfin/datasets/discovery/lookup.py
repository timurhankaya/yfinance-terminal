"""lookup dataset'i (SQ S7.2) -> symbols | lookup_results | lookup_totals.

CAGRI ADAPTIFTIR (SQ K6) ve bu, tasarimin denetimde CURUTULEN ilk
kararidir. Ilk hali "her zaman tek `all` cagrisi" idi ve tek bir DAR
terimle (`BTC`, toplam 503) olculup genellenmisti. Bagimsiz dogrulamada
GENIS terimlerde `all` cagrisinin ~1.000 belgede sertce kirpildigi
gorunulmustur:

    terim   tipli birlesim   `all`   yalniz `all`   yalniz tipli
    BTC              500      500              0              0
    GOLD           3.313      996            354          2.671
    TECH           4.024      998              0          3.026

`GOLD`da fark IKI YONLUDUR: 354 sembol (hepsi `0P...` fon kodlari) yalniz
`all`da, 2.671 sembol yalniz tipli cagrilarda. Yani tipli cagriya gecmek
`all`i BIRAKMAK degil, ONA EKLEMEKTIR.

Sembol dongusunde adaptif dal neredeyse hic tetiklenmez (AAPL 57, THYAO 1),
yani maliyet 1 istek/sembol kalir; bedeli yalnizca genis serbest terimler
oder ve karsiliginda 3-4 kat sembol alir.

`_fetch_lookup` (ham govde) kullanilir, `get_all()` (DataFrame) DEGIL:
`Lookup._parse_response` `lookupTotals` ve `total` alanlarini ATAR
(lookup.py:96-104), oysa `lookup_totals` tablosu, eksiksizlik kaniti VE
adaptif dalin tetikleyicisi onlara dayanir. `_fetch_lookup` sarmalayicinin
KENDI metodudur, yani HTTP'yi ve proxy'yi yine o yapar (SQ K7 korunur).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yfinance as yf

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.config import get_settings
from yfin.datasets.asof_base import asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import (
    dict_items,
    discovered_symbol_row,
    expect_dict,
    symbol_is_writable,
    utc_as_of_day,
)
from yfin.datasets.discovery.base import DISCOVERY_GATE_TABLE, DiscoveryDataset
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

ALL_TYPE = "all"
# `lookup.py:31`deki `LOOKUP_TYPES` sabitinin `all` disindaki uyeleri.
# Sabiti IMPORT ETMEK yerine burada tutmanin sebebi: o liste dokuzuncu tipi
# (`privateCompany`) BILMIYOR ve kutuphane bir gun onu ekledginde bizim
# cagri kumemizin sessizce degismesini istemeyiz.
TYPED_LOOKUPS = (
    "equity",
    "mutualfund",
    "etf",
    "index",
    "future",
    "currency",
    "cryptocurrency",
)

_RESULT_UPDATE = (
    "rank_index",
    "source_rank",
    "lookup_type",
    "quote_type",
    "exchange",
    "short_name",
    "industry_name",
    "industry_link",
    "fullday_price",
    "fullday_change",
    "fullday_change_percent",
    "regular_market_price",
    "regular_market_change",
    "regular_market_percent_change",
    "is_known",
    "fetched_at",
    "raw_json",
)

_TOTAL_UPDATE = ("total", "fetched_at")

# SQ S5.12: `lookup` yalnizca UC tanimlayici alan donduruyor. Ortak bir
# `symbols` update listesi kullanilsaydi bu yol her kosuda `long_name`,
# `currency`, `timezone` ve `full_exchange_name`i NULL'lar, yani
# `search`/`screener`in yazdigini SILERDI.
SYMBOL_UPDATE = ("short_name", "exchange", "quote_type", "last_seen_at")


@dataclass
class LookupPayload:
    query_term: str
    as_of_date: date
    fetched_at: datetime
    # (lookup_type, belge) ciftleri; SIRA korunur
    documents: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)


def _result_block(payload: Any) -> dict[str, Any]:
    """`{"finance": {"result": [ ... ]}}` zarfini acar.

    Zarf sekli degisirse KeyError/TypeError firlatir ve hucre `failed`
    olur; sessizce bos donmek "veri yok" ile "yanit sekli degisti"yi
    birbirine karistirirdi.
    """
    result = expect_dict(payload, what="lookup").get("finance", {}).get("result") or []
    return result[0] if result else {}


def _fetch_type(term: str, lookup_type: str, count: int) -> dict[str, Any]:
    return _result_block(
        call_yahoo(
            lambda: yf.Lookup(term)._fetch_lookup(lookup_type, count),
            what=f"lookup:{term}:{lookup_type}",
        )
    )


class LookupDataset(DiscoveryDataset[LookupPayload]):
    name = "lookup"
    produces = asof_produces(
        "symbols", "lookup_results", "lookup_totals", gate=DISCOVERY_GATE_TABLE
    )

    def fetch(self, ctx: SyncContext) -> LookupPayload:
        cfg = get_settings()
        term = ctx.symbol
        block = _fetch_type(term, ALL_TYPE, cfg.yf_lookup_count)
        totals = {
            str(k): int(v)
            for k, v in (block.get("lookupTotals") or {}).items()
            if isinstance(v, int)
        }
        docs: list[tuple[str, dict[str, Any]]] = [
            (ALL_TYPE, d) for d in dict_items(block, "documents")
        ]

        # SQ K6: `lookupTotals.all` esigi asiyorsa `all` KIRPILMIS demektir.
        # Esik `all`in gozlenen tavaninin (~1.000) ALTINDA tutulur ki
        # kirpilma BASLAMADAN tipli dala gecilsin.
        if totals.get(ALL_TYPE, 0) > cfg.yf_lookup_all_threshold:
            log.info(
                "lookup all kirpildi, tipli dala geciliyor",
                term=term,
                total=totals.get(ALL_TYPE),
                documents=len(docs),
            )
            for lookup_type in TYPED_LOOKUPS:
                typed = _fetch_type(term, lookup_type, cfg.yf_lookup_count)
                docs.extend((lookup_type, d) for d in dict_items(typed, "documents"))

        return LookupPayload(
            query_term=term,
            as_of_date=utc_as_of_day(ctx.fetched_at),
            fetched_at=ctx.fetched_at,
            documents=docs,
            totals=totals,
        )

    def normalize(self, raw: LookupPayload, symbol: str) -> NormalizedResult:
        results: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()

        for index, (lookup_type, doc) in enumerate(raw.documents):
            sym = nz.to_str(doc.get("symbol"))
            if sym is None or sym in seen:
                # Ayni sembol `all` ve tipli cagrida birden donebilir
                # (olculdu: BTC'de uc sembol iki tipte). PK (query_term,
                # as_of_date, symbol) oldugu icin ikinci satir birinciyi
                # ezerdi; ILK gorulen -- yani `all`daki -- korunur.
                continue
            seen.add(sym)
            is_known = symbol_is_writable(sym)
            results.append(
                {
                    "query_term": raw.query_term,
                    "as_of_date": raw.as_of_date,
                    "symbol": sym,
                    "rank_index": index,
                    # Kaynagin KENDI `rank` alani bir sira DEGIL, Yahoo'nun
                    # siralama skorudur (olculen ornek 30007). Kolon adi bu
                    # yuzden ayrildi; KAYNAK anahtari yine `rank`tir.
                    "source_rank": nz.to_int(doc.get("rank")),
                    "lookup_type": lookup_type,
                    "quote_type": nz.to_str(doc.get("quoteType"), max_len=32),
                    "exchange": nz.to_str(doc.get("exchange"), max_len=32),
                    "short_name": nz.to_str(doc.get("shortName"), max_len=128),
                    # YALNIZ `equity` belgelerinde dolu (SQ S4.1/9)
                    "industry_name": nz.to_str(doc.get("industryName"), max_len=128),
                    "industry_link": nz.to_str(doc.get("industryLink")),
                    "fullday_price": nz.to_decimal(doc.get("fulldayPrice")),
                    "fullday_change": nz.to_decimal(doc.get("fulldayChange")),
                    "fullday_change_percent": nz.to_decimal(doc.get("fulldayChangePercent")),
                    "regular_market_price": nz.to_decimal(doc.get("regularMarketPrice")),
                    "regular_market_change": nz.to_decimal(doc.get("regularMarketChange")),
                    "regular_market_percent_change": nz.to_decimal(
                        doc.get("regularMarketPercentChange")
                    ),
                    "is_known": is_known,
                    "fetched_at": raw.fetched_at,
                    "raw_json": nz.canonical_json(doc),
                }
            )
            if is_known:
                symbols.append(
                    discovered_symbol_row(
                        sym,
                        source="lookup",
                        fetched_at=raw.fetched_at,
                        # Lookup belgesi YALNIZ uc tanimlayici alan tasir;
                        # `SYMBOL_UPDATE` da bu ucuyle sinirli (SQ S5.12).
                        short_name=nz.to_str(doc.get("shortName"), max_len=128),
                        exchange=nz.to_str(doc.get("exchange"), max_len=32),
                        quote_type=nz.to_str(doc.get("quoteType"), max_len=32),
                    )
                )

        totals = [
            {
                "query_term": raw.query_term,
                "as_of_date": raw.as_of_date,
                "lookup_type": lookup_type,
                "total": total,
                "fetched_at": raw.fetched_at,
            }
            # Kaynak DOKUZ tip bildiriyor (`privateCompany` dahil) ve
            # `LOOKUP_TYPES` sabiti onu bilmiyor; bu yuzden kume YANITTAN
            # okunur, sabitten degil (SQ S4.1/10).
            for lookup_type, total in sorted(raw.totals.items())
        ]

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="symbols",
                    rows=symbols,
                    key_columns=("symbol",),
                    update_columns=SYMBOL_UPDATE,
                ),
                TableWrite(
                    table="lookup_results",
                    rows=results,
                    key_columns=("query_term", "as_of_date", "symbol"),
                    update_columns=_RESULT_UPDATE,
                ),
                TableWrite(
                    table="lookup_totals",
                    rows=totals,
                    key_columns=("query_term", "as_of_date", "lookup_type"),
                    update_columns=_TOTAL_UPDATE,
                ),
            ]
        )


# OPT-IN: kayitli ama `all` genislemesine GIRMEZ (SQ K11).
# `yfin sync --datasets lookup` calisir; ciplak
# `yfin sync` bu dataset'i CEKMEZ ve maliyeti degismez.
register(LookupDataset(), opt_in=True)
