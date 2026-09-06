"""market_status ve market_summary dataset'leri (S6.5).

`Market.status` YALNIZCA US bolgesinde doludur: `domain/market.py` id
uyusmazligini tespit edip `self._status = None` yapar. Bu deterministiktir,
flaky degil; 7 bolgede `empty` beklenen sonuctur.

`Market.summary` parse hatasinda ham zarf dict'i ({'marketSummaryResponse':
...}) donebilir; board kodu sekli dogrulanmazsa PK'ya cop yazilir.
"""

from __future__ import annotations

from typing import Any

from yfin import normalize as nz
from yfin.client import call_yahoo
from yfin.datasets.base import NormalizedResult, TableWrite, WriteStats
from yfin.datasets.market.base import MarketContext, SnapshotGlobalDataset
from yfin.datasets.payloads import MarketStatusPayload, MarketSummaryPayload
from yfin.datasets.registry import register_market
from yfin.logging_setup import get_logger
from yfin.persistence import RowWriter

log = get_logger(__name__)

BOARD_CODE_MAX = 8

STATUS_COLUMNS = (
    "market_id",
    "name",
    "status",
    "yfit_market_status",
    "message",
    "open_ts_utc",
    "close_ts_utc",
    "timezone_name",
    "gmt_offset",
    "tz_short",
    "raw_json",
    "content_hash",
    "fetched_at",
)

SUMMARY_TYPED = (
    "symbol",
    "is_known",
    "short_name",
    "quote_type",
    "exchange",
    "market_state",
    "currency",
    "regular_market_price",
    "regular_market_change",
    "regular_market_change_percent",
    "regular_market_previous_close",
    "regular_market_ts_utc",
    "exchange_timezone_name",
    "raw_json",
    "content_hash",
    "fetched_at",
)

# market_summary'de tipli kolona alinan quote anahtarlari; kalanlar
# raw_json'da durur ve WARNING ile terfi sinyali verir
_MAPPED_SUMMARY_KEYS = frozenset(
    {
        "symbol",
        "shortName",
        "quoteType",
        "exchange",
        "marketState",
        "currency",
        "regularMarketPrice",
        "regularMarketChange",
        "regularMarketChangePercent",
        "regularMarketPreviousClose",
        "regularMarketTime",
        "exchangeTimezoneName",
    }
)


def _market(mctx: MarketContext, region: str) -> Any:
    from yfinance import Market

    return mctx.cached(f"market:{region}", lambda: Market(region))


class MarketStatusDataset(SnapshotGlobalDataset[MarketStatusPayload]):
    name = "market_status"
    produces = ("market_status", "market_status_history")
    scope = "region"
    snapshot_table = "market_status"
    history_table = "market_status_history"
    key_columns = ("region",)

    def fetch(self, mctx: MarketContext) -> MarketStatusPayload:
        region = mctx.region or "US"
        market = _market(mctx, region)
        status = call_yahoo(lambda: market.status, what=f"market_status:{region}")
        return MarketStatusPayload(region=region, status=status, fetched_at=mctx.fetched_at)

    def normalize(self, raw: MarketStatusPayload) -> NormalizedResult:
        status = raw.status
        if nz.is_empty_result(status):
            # US disindaki 7 bolge: empty, hata DEGIL
            return NormalizedResult()
        payload = nz.as_mapping(status)
        timezone = payload.get("timezone")
        tz_payload = nz.as_mapping(timezone) if isinstance(timezone, dict) else {}

        row: dict[str, Any] = {
            "region": raw.region,
            "market_id": nz.to_str(payload.get("id"), max_len=32),
            "name": nz.to_str(payload.get("name"), max_len=64),
            "status": nz.to_str(payload.get("status"), max_len=32),
            "yfit_market_status": nz.to_str(payload.get("yfit_market_status"), max_len=64),
            "message": nz.to_str(payload.get("message")),
            "open_ts_utc": nz.to_datetime_utc(payload.get("open")),
            "close_ts_utc": nz.to_datetime_utc(payload.get("close")),
            "timezone_name": nz.to_str(tz_payload.get("$text"), max_len=64),
            "gmt_offset": nz.to_int(tz_payload.get("gmtoffset")),
            "tz_short": nz.to_str(payload.get("tz"), max_len=16),
        }
        # open/close birer datetime nesnesidir: duz json.dumps TypeError verir
        canonical = nz.canonical_json(dict(payload))
        row["raw_json"] = canonical
        row["content_hash"] = nz.content_hash(canonical=canonical)
        row["fetched_at"] = raw.fetched_at

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="market_status",
                    rows=[dict(row)],
                    key_columns=("region",),
                    update_columns=STATUS_COLUMNS,
                ),
                TableWrite(
                    table="market_status_history",
                    rows=[dict(row)],
                    key_columns=("region", "fetched_at"),
                    update_columns=STATUS_COLUMNS,
                ),
            ]
        )


class MarketSummaryDataset(SnapshotGlobalDataset[MarketSummaryPayload]):
    name = "market_summary"
    produces = ("market_summary", "market_summary_history")
    scope = "region"
    snapshot_table = "market_summary"
    history_table = "market_summary_history"
    key_columns = ("region", "board_code")

    def fetch(self, mctx: MarketContext) -> MarketSummaryPayload:
        region = mctx.region or "US"
        market = _market(mctx, region)
        summary = call_yahoo(lambda: market.summary, what=f"market_summary:{region}")
        return MarketSummaryPayload(region=region, summary=summary, fetched_at=mctx.fetched_at)

    def normalize(self, raw: MarketSummaryPayload) -> NormalizedResult:
        summary = raw.summary
        if nz.is_empty_result(summary):
            return NormalizedResult()
        payload = nz.as_mapping(summary)

        # Parse hatasinda yfinance ham zarf dict'i dondurebilir; sekil
        # dogrulanmazsa (region, board_code) PK'sina cop yazilir
        for key, value in payload.items():
            if len(str(key)) > BOARD_CODE_MAX or not isinstance(value, dict):
                raise ValueError(f"market summary sekli beklenmedik: anahtar={key!r}")

        rows: list[dict[str, Any]] = []
        symbols: set[str] = set()
        for board_code, quote in payload.items():
            quote_map = nz.as_mapping(quote)
            unmapped = sorted(set(quote_map) - _MAPPED_SUMMARY_KEYS)
            if unmapped:
                log.warning(
                    "unmapped market summary keys",
                    region=raw.region,
                    board=board_code,
                    keys=unmapped[:10],
                )
            symbol = nz.to_str(quote_map.get("symbol"), max_len=32)
            if symbol:
                symbol = nz.normalize_symbol(symbol)
                symbols.add(symbol)
            canonical = nz.canonical_json(dict(quote_map))
            rows.append(
                {
                    "region": raw.region,
                    "board_code": str(board_code),
                    "symbol": symbol,
                    "is_known": False,
                    "short_name": nz.to_str(quote_map.get("shortName"), max_len=64),
                    "quote_type": nz.to_str(quote_map.get("quoteType"), max_len=32),
                    "exchange": nz.to_str(quote_map.get("exchange"), max_len=32),
                    "market_state": nz.to_str(quote_map.get("marketState"), max_len=16),
                    "currency": nz.to_str(quote_map.get("currency"), max_len=8),
                    "regular_market_price": nz.to_decimal(quote_map.get("regularMarketPrice")),
                    "regular_market_change": nz.to_decimal(quote_map.get("regularMarketChange")),
                    "regular_market_change_percent": nz.to_decimal(
                        quote_map.get("regularMarketChangePercent")
                    ),
                    "regular_market_previous_close": nz.to_decimal(
                        quote_map.get("regularMarketPreviousClose")
                    ),
                    "regular_market_ts_utc": nz.epoch_to_datetime(
                        quote_map.get("regularMarketTime"), unit="s"
                    ),
                    "exchange_timezone_name": nz.to_str(
                        quote_map.get("exchangeTimezoneName"), max_len=64
                    ),
                    "raw_json": canonical,
                    "content_hash": nz.content_hash(canonical=canonical),
                    "fetched_at": raw.fetched_at,
                }
            )

        return NormalizedResult(
            writes=[
                TableWrite(
                    table="market_summary",
                    rows=[dict(r) for r in rows],
                    key_columns=("region", "board_code"),
                    update_columns=SUMMARY_TYPED,
                ),
                TableWrite(
                    table="market_summary_history",
                    rows=[dict(r) for r in rows],
                    key_columns=("region", "board_code", "fetched_at"),
                    update_columns=SUMMARY_TYPED,
                ),
            ]
        )

    def upsert(self, writer: RowWriter, result: NormalizedResult) -> WriteStats:
        # Board sembolleri (ES=F, ^GSPC) evrende olmayabilir: FK yok,
        # is_known bayragi isaretlenir (news_symbols ile ayni desen)
        candidates = {
            row["symbol"] for write in result.writes for row in write.rows if row.get("symbol")
        }
        known = writer.known_symbols(candidates) if candidates else set()
        for write in result.writes:
            for row in write.rows:
                row["is_known"] = bool(row.get("symbol")) and row["symbol"] in known
        return super().upsert(writer, result)


register_market(MarketStatusDataset())
register_market(MarketSummaryDataset())
