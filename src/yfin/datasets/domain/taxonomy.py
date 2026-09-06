"""domain_taxonomy -- bootstrap dataset'i (SI S7.2).

Bolgesiz ve as-of DEGIL: duz upsert. Ad, aciklama, sembol ve ebeveyn yilda
birkac kez degisir; gunluk anlik goruntu almanin karsiligi yoktur.

TEK TURDA kosar (`per_key = False`): 156 `symbols` + 156 `domains` satiri
tek transaction'da yazilir -- taksonomi ya butun olarak tutarlidir ya hic.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from yfin.datasets.base import NormalizedResult, TableWrite
from yfin.datasets.domain.base import DomainContext, DomainDataset
from yfin.datasets.domain.common import (
    DOMAINS_TABLE,
    SECTOR_KEYS,
    SYMBOLS_TABLE,
    fetch_domain,
    text_of,
)
from yfin.datasets.domain.payloads import TaxonomyPayload
from yfin.datasets.registry import register_domain
from yfin.logging_setup import get_logger

log = get_logger(__name__)

# 6/6 domain sembolunde AYNI olculdu (ayri bir canli `fast_info` olcumu;
# bu alanlar sector/industry yanitinda YOKTUR).
DOMAIN_QUOTE_TYPE = "INDEX"
DOMAIN_EXCHANGE = "YHD"
DOMAIN_CURRENCY = "USD"
DOMAIN_TIMEZONE = "America/New_York"

# `is_active` ve `unknown_streak` update_columns DISINDADIR: kullanici
# `^YH311`i elle etkinlestirmisse bir sonraki domain sync onu GERI
# KAPATMAZ (SI S7.8).
SYMBOL_UPDATE_COLUMNS = (
    "short_name",
    "quote_type",
    "exchange",
    "currency",
    "timezone",
    "last_seen_at",
)
# `first_seen_at` DISARIDA (AH S5.4). `description` / `message_board_id` de
# disaridadir: endustri tarafinda onlari `industry_profile` yazar ve iki
# yazicinin AYRIK kolon kumeleri guncellemesi birbirini ezmelerini onler
# (SI S5.11).
DOMAIN_UPDATE_COLUMNS = ("symbol", "parent_key", "name", "fetched_at")


class DomainTaxonomyDataset(DomainDataset[TaxonomyPayload]):
    name = "domain_taxonomy"
    scope = "sector"
    regional = False
    per_key = False
    produces = (SYMBOLS_TABLE, DOMAINS_TABLE)

    def fetch(self, ctx: DomainContext) -> TaxonomyPayload:
        region = ctx.fetch_region
        sectors: dict[str, dict[str, Any]] = {}
        for key in SECTOR_KEYS:
            # `partial`, dongu degiskenini lambda varsayilanina baglamaktan
            # daha durustur ve mypy da onu cozebiliyor.
            sectors[key] = ctx.cached(
                f"raw:{key}:{region}", partial(fetch_domain, key, "sector", region)
            )
        return TaxonomyPayload(sectors=sectors, fetched_at=ctx.fetched_at)

    def normalize(self, raw: TaxonomyPayload, key: str) -> NormalizedResult:
        fetched_at = raw.fetched_at
        symbol_rows: list[dict[str, Any]] = []
        domain_rows: list[dict[str, Any]] = []

        for sector_key in SECTOR_KEYS:
            data = raw.sectors.get(sector_key)
            if not data:
                continue
            overview = data.get("overview") or {}
            sector_symbol = text_of(data, "symbol", 32)
            sector_name = text_of(data, "name", 64)
            if sector_symbol is None or sector_name is None:
                # `symbol` UNIQUE NOT NULL, `name` NOT NULL: eksikse satir
                # ERROR 1048 verir ve TURUN tamamini dusururdu.
                log.warning("sektor kimlik alani eksik", domain_key=sector_key)
                continue

            symbol_rows.append(_symbol_row(sector_symbol, sector_name, fetched_at))
            domain_rows.append(
                {
                    "domain_key": sector_key,
                    "domain_type": "sector",
                    "symbol": sector_symbol,
                    "parent_key": None,
                    "name": sector_name,
                    "description": text_of(overview, "description"),
                    "message_board_id": text_of(overview, "messageBoardId", 32),
                    "first_seen_at": fetched_at,
                    "fetched_at": fetched_at,
                }
            )

            # SATIR SIRASI TEK `TableWrite` ICINDE BAGLAYICIDIR: `parent_key`
            # bir SELF-FK'dir, bu yuzden sektorun satiri kendi
            # endustrilerinden ONCE gelmelidir. `apply_write` satirlari
            # verilen sirayla gonderir.
            for row in data.get("industries") or []:
                # "ALL INDUSTRIES" ELEME KURALI: `key` ALANININ YOKLUGU.
                # yfinance `i.get('name') != 'All Industries'` ile eliyor;
                # bu ADA bakan, DILE BAGLI bir kural. Olcum: 13 satirin
                # 12'sinde `key` ve `symbol` var, o satirda ikisi de yok.
                # Elenen satirin verisi KAYBOLMAZ: degerleri `performance`
                # bloguyla ozdes olculdu.
                industry_key = text_of(row, "key", 48)
                if industry_key is None:
                    continue
                industry_symbol = text_of(row, "symbol", 32)
                industry_name = text_of(row, "name", 64)
                if industry_symbol is None or industry_name is None:
                    log.warning("endustri kimlik alani eksik", domain_key=industry_key)
                    continue
                symbol_rows.append(_symbol_row(industry_symbol, industry_name, fetched_at))
                domain_rows.append(
                    {
                        "domain_key": industry_key,
                        "domain_type": "industry",
                        "symbol": industry_symbol,
                        "parent_key": sector_key,
                        "name": industry_name,
                        # `industries[]` blogunda BU IKI ALAN YOKTUR
                        # (SI S4.3); endustri icin onlari
                        # `industry_profile` doldurur.
                        "description": None,
                        "message_board_id": None,
                        "first_seen_at": fetched_at,
                        "fetched_at": fetched_at,
                    }
                )

        return NormalizedResult(
            writes=[
                # TABLO SIRASI BAGLAYICIDIR: `symbols` yazimi
                # `domains.symbol` FK'sinden ONCE gelmelidir.
                TableWrite(
                    table=SYMBOLS_TABLE,
                    rows=symbol_rows,
                    key_columns=("symbol",),
                    update_columns=SYMBOL_UPDATE_COLUMNS,
                ),
                TableWrite(
                    table=DOMAINS_TABLE,
                    rows=domain_rows,
                    key_columns=("domain_key",),
                    update_columns=DOMAIN_UPDATE_COLUMNS,
                ),
            ]
        )


def _symbol_row(symbol: str, name: str, fetched_at: Any) -> dict[str, Any]:
    """`symbols` satiri; `is_active` ACIKCA 0 verilir (server_default '1').

    Sektor/endustri indeksleri varsayilan `yfin sync` evrenine GIRMEZ;
    kullanici isterse `--include-inactive` ya da `--quote-type INDEX` ile
    onlarin fiyat gecmisini ve `info`/`fast_info` verisini mevcut
    dataset'lerle toplayabilir (`^YH311.history(period='5d')` -> (5,7)
    olculdu).
    """
    return {
        "symbol": symbol,
        "short_name": name[:128],
        "quote_type": DOMAIN_QUOTE_TYPE,
        "exchange": DOMAIN_EXCHANGE,
        "currency": DOMAIN_CURRENCY,
        "timezone": DOMAIN_TIMEZONE,
        "is_active": False,
        "last_seen_at": fetched_at,
    }


register_domain(DomainTaxonomyDataset())
