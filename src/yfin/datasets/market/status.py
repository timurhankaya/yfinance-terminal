"""market_status and market_summary datasets.

`Market.status` is populated ONLY for the US region: `domain/market.py`
detects an id mismatch and sets `self._status = None`. This is
deterministic, not flaky; `empty` is the expected result for the other 7
regions.

On a parse error, `Market.summary` can return the raw envelope dict
({'marketSummaryResponse': ...}); without validating the board code's
shape, garbage gets written into the PK.
"""

from __future__ import annotations

from typing import Any

from yfin.core import normalize as nz
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import NormalizedResult
from yfin.datasets.market.base import MarketContext, SnapshotGlobalDataset
from yfin.datasets.payloads import MarketStatusPayload, MarketSummaryPayload
from yfin.datasets.registry import register_market
from yfin.ingest.client import call_yahoo
from yfin.storage.contracts import RowWriter, TableWrite, WriteStats

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

# Quote keys given typed columns in market_summary; the rest stay in
# raw_json and trigger a WARNING signal.
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
            # The 7 non-US regions: empty, NOT an error.
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
        # open/close are datetime objects: plain json.dumps would raise TypeError.
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

        # On a parse error, yfinance can return the raw envelope dict;
        # without validating shape, garbage gets written into the
        # (region, board_code) PK.
        for key, value in payload.items():
            if len(str(key)) > BOARD_CODE_MAX or not isinstance(value, dict):
                raise ValueError(f"unexpected market summary shape: key={key!r}")

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
        # Board symbols (ES=F, ^GSPC) may be outside the universe: no FK,
        # the is_known flag is marked instead (same pattern as news_symbols).
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
