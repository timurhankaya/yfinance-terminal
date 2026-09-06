"""screener dataset'i (SQ S7.3) -> screens | screen_runs | screen_members |
screen_quotes | symbols.

`scope="variant"`: ekran dongusu dataset'in DISINDA doner (SQ K2), tipki
bolge dongusu gibi. Boylece `sync_run_items` granulerligi dogal olarak
(dataset x ekran x tablo) olur ve bir ekranin patlamasi komsu ekrani
`failed` gostermez.

Kapi `HashGate` mixin'iyle kurulur, `HashGatedDataset` ILE DEGIL: o sinif
`Dataset[RawT]`in altindadir ve `fetch(SyncContext)` imzasini tasir; burada
`GlobalDataset` hiyerarsisi vardir (SQ S6.2). Kapi mantigi ikisinde de
ayni oldugu icin mixin paylasilir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import yfinance as yf

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.config import Settings
from yfin.datasets.base import NormalizedResult, TableWrite
from yfin.datasets.common import project_fields, warn_unmapped
from yfin.datasets.hash_gated import HashGate
from yfin.datasets.market.base import GlobalDataset, MarketContext
from yfin.datasets.registry import register_market
from yfin.logging_setup import get_logger
from yfin.models.fields import SCREENER_NON_COLUMN_SOURCES, SCREENER_QUOTE_FIELDS
from yfin.screens import ALL_SCREENS, ScreenDef, screen_by_key

log = get_logger(__name__)

GATE_TABLE = "screen_runs"
CHILD_TABLE = "screen_members"
GATE_KEY_COLUMNS = ("screen_key", "as_of_date")

_SCREEN_UPDATE = (
    "kind",
    "quote_type",
    "title",
    "description",
    "sort_field",
    "sort_asc",
    "definition_json",
    "updated_at",
)
# `is_enabled` KAPSAM DISI: operator DB'de kapattiginda her kosu onu geri
# acardi. `created_at` de disaridadir (yalniz INSERT'te yazilir).

_RUN_UPDATE = (
    "total",
    "fetched_rows",
    "row_count",
    "page_count",
    "yahoo_id",
    "version_id",
    "last_updated",
    "criteria_json",
    "content_hash",
    "fetched_at",
)

_MEMBER_UPDATE = ("rank_index", "is_known", "fetched_at")

_QUOTE_UPDATE = tuple(f.column for f in SCREENER_QUOTE_FIELDS) + (
    "is_known",
    "fetched_at",
    "raw_json",
)

# SQ K10: kesif yazimi `is_active`, `unknown_streak`, `discovered_by` ve
# `discovered_at`i GUNCELLEMEZ. Aksi halde operatorun elle aktiflestirdigi
# sembol ertesi gun ayni ekranda gorulup SESSIZCE pasife donerdi.
#
# Liste, screener'in GERCEKTEN doldurdugu kolonlarla sinirlidir (SQ S5.12):
# ortak bir liste kullanilsaydi `lookup` yolu `long_name`/`currency`yi
# NULL'lardi.
SYMBOL_UPDATE = (
    "short_name",
    "long_name",
    "exchange",
    "full_exchange_name",
    "quote_type",
    "currency",
    "timezone",
    "first_trade_date",
    "last_seen_at",
)


@dataclass
class ScreenPage:
    """Tek bir `yf.screen` yaniti."""

    quotes: list[dict[str, Any]]
    total: int
    # Yalniz predefined ILK sayfada (GET) dolu; POST yaniti 5 anahtar
    # tasir ve bunlarin hicbirini icermez (SQ S4.1/13).
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScreenPayload:
    screen_key: str
    as_of_date: date
    fetched_at: datetime
    quotes: list[dict[str, Any]]
    total: int
    page_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


def _fetch_page(
    spec: ScreenDef, *, offset: int | None, size: int
) -> ScreenPage:
    """Tek sayfa ceker.

    SQ K12 -- ILK sayfa `count`, sonrakiler `size`. `offset` verildiginde
    `yf.screen` predefined GET yolundan custom POST yoluna geciyor ve
    `count`u SESSIZCE yok sayiyor: `offset=250, count=250` 25 satir
    dondurdu, `size=250` ise 250. Hata VERILMEZ; tek parametre adiyla
    yazilsaydi sayfa basina 225 satir kaybedilir ve kimse fark etmezdi.

    SQ K15 -- `sortField`/`sortAsc` HER istekte acikca verilir. `sortAsc`
    varsayilani None -> azalan; sayfalar arasi sira kararli olmazsa
    sayfalar ortusur ya da sembol atlanir.
    """
    query: Any = spec.key if spec.kind == "predefined" else spec.query
    if offset is None:
        raw = call_yahoo(
            lambda: yf.screen(
                query, count=size, sortField=spec.sort_field, sortAsc=spec.sort_asc
            ),
            what=f"screen:{spec.key}:p0",
        )
    else:
        raw = call_yahoo(
            lambda: yf.screen(
                query,
                offset=offset,
                size=size,
                sortField=spec.sort_field,
                sortAsc=spec.sort_asc,
            ),
            what=f"screen:{spec.key}:@{offset}",
        )
    if not isinstance(raw, dict):  # pragma: no cover - savunma
        raise TypeError(f"screen yaniti sozluk degil: {type(raw).__name__}")

    quotes = [q for q in raw.get("quotes") or [] if isinstance(q, dict)]
    metadata = {k: v for k, v in raw.items() if k != "quotes"}
    return ScreenPage(quotes=quotes, total=nz.to_int(raw.get("total")) or 0, metadata=metadata)


class ScreenerDataset(HashGate, GlobalDataset[ScreenPayload]):
    name = "screener"
    scope = "variant"
    produces = (
        "screens",
        "symbols",
        "screen_quotes",
        GATE_TABLE,
        CHILD_TABLE,
    )

    gate_table = GATE_TABLE
    child_table = CHILD_TABLE
    gate_key_columns = GATE_KEY_COLUMNS

    # --- dis dongu ---------------------------------------------------------

    def variants(self, settings: Any, session: Any) -> list[str]:
        """Ekran kumesi `screens.py`den, ETKINLIK DB'den (SQ S6.5).

        Yon onemlidir: kume KODDAN gelir, DB yalnizca ELER. Tersi olsaydi
        (kume DB'den) bos `screens` tablosuyla hicbir ekran hic kosmaz ve
        seed edilmeden once bootstrap kilitlenirdi -- tablo da yalnizca
        kosu sirasinda dolduguna gore, kilit hic acilmazdi.
        """
        cfg: Settings = settings
        wanted = [k.strip() for k in cfg.yf_screen_keys.split(",") if k.strip()]
        keys = [s.key for s in ALL_SCREENS]
        if wanted:
            unknown = [k for k in wanted if k not in set(keys)]
            if unknown:
                raise ValueError(
                    f"bilinmeyen ekran: {', '.join(unknown)}. "
                    f"gecerli adlar: {', '.join(sorted(keys))}"
                )
            keys = [k for k in keys if k in set(wanted)]
        return [k for k in keys if k not in _disabled_keys(session)]

    # --- fetch -------------------------------------------------------------

    def fetch(self, mctx: MarketContext) -> ScreenPayload:
        from yfin.config import get_settings

        cfg = get_settings()
        if mctx.variant is None:  # pragma: no cover - savunma
            raise ValueError("screener `variant` olmadan cagrilamaz")
        spec = screen_by_key(mctx.variant)

        quotes: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        total = 0
        offset = 0
        pages = 0

        while pages < cfg.yf_screen_max_pages:
            page = _fetch_page(spec, offset=None if pages == 0 else offset, size=cfg.yf_screen_size)
            pages += 1
            if pages == 1:
                # ILK sayfa ayricalikli: `title`, `description`,
                # `rawCriteria`, `lastUpdated` yalnizca predefined GET
                # yanitindadir. Custom ekranda ilk sayfa da POST'tur ve
                # metadata GELMEZ (olculdu) -- o zaman `ScreenDef` konusur.
                metadata = page.metadata
                total = page.total
            quotes.extend(page.quotes)
            # Durma UC DALLI: bos sayfa / offset >= total / sayfa siniri.
            # `offset > total` durumunda Yahoo hata vermeden 0 satir
            # donduruyor, yani bos-sayfa dali tek basina da yeterdi; `total`
            # kontrolu GEREKSIZ BIR ISTEGI daha bastan engeller.
            if not page.quotes:
                break
            offset += len(page.quotes)
            if offset >= page.total:
                break

        return ScreenPayload(
            screen_key=spec.key,
            as_of_date=datetime.now(UTC).date(),
            fetched_at=mctx.fetched_at,
            quotes=quotes,
            total=total,
            page_count=pages,
            metadata=metadata,
        )

    # --- normalize ---------------------------------------------------------

    def normalize(self, raw: ScreenPayload) -> NormalizedResult:
        spec = screen_by_key(raw.screen_key)
        members: list[dict[str, Any]] = []
        quotes: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()

        for index, quote in enumerate(raw.quotes):
            symbol = nz.to_str(quote.get("symbol"))
            if symbol is None:
                # `screen` 300 olculen satirin hepsinde `symbol` dondurdu;
                # yine de PK'ya NULL yazmaktansa satiri elemek dogru.
                continue
            is_known = _symbol_is_writable(symbol)
            members.append(
                {
                    "screen_key": raw.screen_key,
                    "as_of_date": raw.as_of_date,
                    "symbol": symbol,
                    # `offset + sayfa ici indeks` -> MUTLAK sira.
                    # `enumerate` bunu dogal olarak verir cunku sayfalar
                    # SIRAYLA eklendi (SQ S5.14).
                    "rank_index": index,
                    "is_known": is_known,
                    "fetched_at": raw.fetched_at,
                }
            )
            if symbol in seen:
                # Ayni sembol iki sayfada gorunurse (sira kaymasi) kotasyonu
                # bir kez yazilir; uyelik satiri PK sayesinde zaten tekil.
                continue
            seen.add(symbol)
            quotes.append(_quote_row(symbol, quote, raw, is_known=is_known))
            if is_known:
                symbols.append(_symbol_row(symbol, quote, raw))

        writes = [
            TableWrite(
                table="screens",
                rows=[_screen_row(spec, raw)],
                key_columns=("screen_key",),
                update_columns=_SCREEN_UPDATE,
            ),
            TableWrite(
                table="symbols",
                rows=symbols,
                key_columns=("symbol",),
                update_columns=SYMBOL_UPDATE,
            ),
            TableWrite(
                table="screen_quotes",
                rows=quotes,
                key_columns=("symbol", "as_of_date"),
                update_columns=_QUOTE_UPDATE,
            ),
            TableWrite(
                table=GATE_TABLE,
                rows=[_run_row(raw, members)],
                key_columns=GATE_KEY_COLUMNS,
                update_columns=_RUN_UPDATE,
            ),
            TableWrite(
                table=CHILD_TABLE,
                rows=members,
                key_columns=(*GATE_KEY_COLUMNS, "symbol"),
                update_columns=_MEMBER_UPDATE,
            ),
        ]
        return NormalizedResult(writes=writes)


def _disabled_keys(session: Any) -> set[str]:
    """DB'de ACIKCA kapatilmis ekranlar."""
    from sqlalchemy import select

    from yfin.models.discovery import Screen

    if session is None:  # kutuphane kullanimi / testler
        return set()
    stmt = select(Screen.screen_key).where(Screen.is_enabled.is_(False))
    return set(session.execute(stmt).scalars())


def _symbol_is_writable(symbol: str) -> bool:
    """`SymbolType()` = VARCHAR(32) ascii_bin kisitina uyuyor mu (SQ S8.3).

    Yazma SIRASINDAN turetilmez; `normalize` icinde hesaplanir. Sirayla
    turetilseydi `symbols` yaziminin `writes` listesindeki yeri anlam
    tasirdi ve kapi kapsami duzeltmesiyle sessizce bozulurdu.
    """
    return len(symbol) <= 32 and symbol.isascii()


def _screen_row(spec: ScreenDef, raw: ScreenPayload) -> dict[str, Any]:
    """`screens` satiri; metadata varsa ILK GET sayfasindan TAZELENIR."""
    meta = raw.metadata
    title = nz.to_str(meta.get("title")) or spec.title
    description = nz.to_str(meta.get("description")) or spec.description or None
    if spec.kind == "custom":
        definition = nz.canonical_json(spec.query.to_dict()) if spec.query else None
    else:
        definition = nz.canonical_json(meta.get("rawCriteria")) if meta.get("rawCriteria") else None
    return {
        "screen_key": spec.key,
        "kind": spec.kind,
        "quote_type": spec.quote_type,
        "title": title,
        "description": description,
        "sort_field": spec.sort_field,
        "sort_asc": spec.sort_asc,
        "definition_json": definition,
        "is_enabled": spec.is_enabled,
        "created_at": raw.fetched_at,
        "updated_at": raw.fetched_at,
    }


def _run_row(raw: ScreenPayload, members: list[dict[str, Any]]) -> dict[str, Any]:
    """Kapi + veri satiri.

    `content_hash` YALNIZ KADROYU kapsar (SQ K4): `(symbol, rank_index)` ciftleri.
    Kotasyon metrikleri govdeye girseydi `regularMarketPrice` her kosuda
    oynadigi icin hash HICBIR ZAMAN esitlenmez, `skipped` durumu hic
    uretilmez ve mekanizma sessizce olurdu -- kimse fark etmezdi cunku
    sonuc "her gun her satir yeniden yazildi" olurdu.

    `rank_index` govdededir: kadro ayni kalip SIRA degistiginde bu GERCEK bir
    degisimdir ve yazilmalidir.
    """
    body = [{"symbol": m["symbol"], "rank_index": m["rank_index"]} for m in members]
    meta = raw.metadata
    return {
        "screen_key": raw.screen_key,
        "as_of_date": raw.as_of_date,
        "total": raw.total,
        "fetched_rows": len(raw.quotes),
        "row_count": len(members),
        "page_count": raw.page_count,
        "yahoo_id": nz.to_str(meta.get("id")),
        "version_id": nz.to_int(meta.get("versionId")),
        "last_updated": nz.epoch_to_datetime(meta.get("lastUpdated"), unit="ms"),
        "criteria_json": nz.canonical_json(meta.get("criteriaMeta"))
        if meta.get("criteriaMeta")
        else None,
        "content_hash": nz.content_hash(canonical=nz.canonical_json(body)),
        "fetched_at": raw.fetched_at,
    }


def _quote_row(
    symbol: str, quote: dict[str, Any], raw: ScreenPayload, *, is_known: bool
) -> dict[str, Any]:
    warn_unmapped(
        quote,
        SCREENER_QUOTE_FIELDS,
        dataset="screener",
        ignore=SCREENER_NON_COLUMN_SOURCES,
    )
    return {
        "symbol": symbol,
        "as_of_date": raw.as_of_date,
        **project_fields(quote, SCREENER_QUOTE_FIELDS),
        "is_known": is_known,
        # `corporateActions` LISTEDIR ve kolona cikmaz; burada durur.
        "raw_json": nz.canonical_json(quote),
        "fetched_at": raw.fetched_at,
    }


def _symbol_row(symbol: str, quote: dict[str, Any], raw: ScreenPayload) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "short_name": nz.to_str(quote.get("shortName"), max_len=128),
        "long_name": nz.to_str(quote.get("longName"), max_len=255),
        "exchange": nz.to_str(quote.get("exchange"), max_len=32),
        "full_exchange_name": nz.to_str(quote.get("fullExchangeName"), max_len=64),
        "quote_type": nz.to_str(quote.get("quoteType"), max_len=32),
        "currency": nz.to_str(quote.get("currency"), max_len=32),
        "timezone": nz.to_str(quote.get("exchangeTimezoneName"), max_len=64),
        "first_trade_date": nz.epoch_to_datetime(
            quote.get("firstTradeDateMilliseconds"), unit="ms"
        ),
        # SADECE INSERT'te etkili (K10): update_columns bunlari kapsamaz.
        "is_active": False,
        "discovered_by": "screener",
        "discovered_at": raw.fetched_at,
        "last_seen_at": raw.fetched_at,
    }


register_market(ScreenerDataset())
