"""Multi-interval bar fetch -> price_bars.

Includes the window planner, the dataset class, and normalize.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd

from yfin.core import normalize as nz
from yfin.core.config import get_settings
from yfin.core.errors import DatasetOutOfScope, is_no_data
from yfin.core.logging_setup import get_logger
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.registry import register
from yfin.ingest.client import call_yahoo
from yfin.models.bars import (
    BAR_INTERVALS,
    GAP_FETCH_FAILED,
    GAP_RETENTION_EXPIRED,
    INTRADAY_INTERVALS,
    bars_table_for,
)
from yfin.storage.contracts import TableWrite

# (max days per request, max lookback depth in days) - measured values.
#
# These are NOT the limits Yahoo advertises, but the ones measurement found
# accepted: Yahoo's messages say "60 days" / "730 days", but exactly 60 and
# 730 were rejected while 59 and 729 were accepted. The limits are second-
# based, not day-based, and relative to "now," so an exact boundary is
# always risky. The margin already lives here; plan_windows does not add a
# second one on top.
#
# None = unbounded: for 1wk/1mo the first fill uses period="max".
log = get_logger(__name__)

BAR_LIMITS: dict[str, tuple[int | None, int | None]] = {
    # 1m depth is 29, not 30: Yahoo's message says "within the last 30
    # days" but exactly 30 days is rejected (measured: -30d REJECTED, -29d
    # 1950 bars). Same pattern as 60 being rejected for 5m and 730 for 60m.
    # This value was left at 30 in the first cut and caught on the first
    # live run: the planner produced a request right at the boundary, got
    # YFPricesMissingError, and AAPL's entire 1m initial fill was dropped.
    "1m": (8, 29),
    "5m": (59, 59),
    "15m": (59, 59),
    "60m": (729, 729),
    "1wk": (None, None),
    "1mo": (None, None),
}


@dataclass(frozen=True)
class FetchPlan:
    """One run's fetch plan for a single interval.

    If `windows` is empty and `gap` is None: first fill of an unbounded
    interval, and the caller uses period="max".
    """

    # [start, end) half-open ranges; yfinance's `end` parameter is also
    # exclusive, so consecutive slices do not overlap.
    windows: tuple[tuple[date, date], ...] = ()
    # Range no longer fetchable because it fell outside Yahoo's retention
    # window. Written to bar_gaps as 'retention_expired'.
    gap: tuple[datetime, datetime] | None = None


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _slice(start: date, end: date, per_request: int | None) -> list[tuple[date, date]]:
    """Splits [start, end) into request-sized windows."""
    if end <= start:
        return []
    if per_request is None:
        return [(start, end)]
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=per_request), end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


def plan_windows(
    interval: str,
    watermark: datetime | None,
    now: datetime,
    *,
    start: date | None = None,
    end: date | None = None,
    overlap_days: int = 2,
    open_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> FetchPlan:
    """Computes the windows to fetch and any unrecoverable gap for one interval.

    Five scenarios:
      1. First fill (no watermark): back to the depth limit, sliced into windows.
      2. Normal increment: watermark - overlap, single slice.
      3. Resume after a pause: a range past the per-request limit is split into
         multiple slices -- a single naive slice would be rejected by Yahoo
         and lose all the data.
      4. Depth exceeded: the fetchable part is sliced, the rest becomes `gap`.
      5. Open gaps: any still within the retention window are retried; without
         this feedback, bar_gaps would just be a tombstone list.

    If `start`/`end` are given (--start/--end, date_range="api"), watermark
    and `open_gaps` are ignored and no gap is produced for depth overrun: a
    manually requested backfill failing is user error, not a missed fetch.
    """
    if interval not in BAR_LIMITS:
        raise ValueError(f"unknown interval: {interval}; valid: {', '.join(BAR_INTERVALS)}")

    per_request, depth = BAR_LIMITS[interval]
    today = _as_date(now)
    earliest = today - timedelta(days=depth) if depth is not None else None

    # --- manual range: watermark and open gaps disabled -------------------
    if start is not None or end is not None:
        window_start = start if start is not None else today - timedelta(days=overlap_days)
        window_end = end if end is not None else today
        return FetchPlan(windows=tuple(_slice(window_start, window_end, per_request)))

    # --- first fill ---------------------------------------------------------
    if watermark is None:
        if earliest is None:
            # Unbounded interval: period="max" (no slicing)
            return FetchPlan()
        return FetchPlan(windows=tuple(_slice(earliest, today, per_request)))

    # --- incremental ----------------------------------------------------------
    wanted_start = _as_date(watermark) - timedelta(days=overlap_days)
    gap: tuple[datetime, datetime] | None = None
    if earliest is not None and wanted_start < earliest:
        # Past Yahoo's retention window: the unrecoverable part is recorded.
        # The gap ends at the first window's start; otherwise a day in
        # between would be neither fetched nor recorded.
        gap = (watermark, datetime.combine(earliest, datetime.min.time(), tzinfo=watermark.tzinfo))
        wanted_start = earliest

    # Watermark range plus any open gaps still within the retention window.
    # Gaps are added as separate ranges: slicing one range from min(all) to
    # today would re-fetch every day in between for a gap 25 days old.
    ranges: list[tuple[date, date]] = [(wanted_start, today)]
    for gap_start, gap_end_ts in open_gaps:
        gap_from = _as_date(gap_start)
        if earliest is not None and gap_from < earliest:
            continue  # retention window closed; do not request it
        # Gap end is extended by one day to include it: yfinance's `end`
        # parameter is exclusive.
        gap_to = _as_date(gap_end_ts) + timedelta(days=1)
        ranges.append((gap_from, min(gap_to, today)))

    windows: list[tuple[date, date]] = []
    for range_start, range_end in _merge(ranges):
        windows.extend(_slice(range_start, range_end, per_request))
    return FetchPlan(windows=tuple(windows), gap=gap)


def _merge(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Merges overlapping/adjacent ranges.

    Adjacent ranges are merged too (`<=`): if a gap butts right up against
    the watermark window, one request suffices instead of two.
    """
    ordered = sorted(r for r in ranges if r[1] > r[0])
    if not ordered:
        return []
    merged = [ordered[0]]
    for current_start, current_end in ordered[1:]:
        last_start, last_end = merged[-1]
        if current_start <= last_end:
            merged[-1] = (last_start, max(last_end, current_end))
        else:
            merged.append((current_start, current_end))
    return merged


@dataclass(frozen=True)
class BarPayload:
    """Raw data returned by fetch.

    No `has_prepost` field, deliberately. The first design used
    has_pre_post_market_data as an early-exit gate; measurement discredited
    it: SHEL.L and VWCE.DE report False for that field yet still return 5
    and 8 extended-hours bars. That gate would have counted them as regular
    session and let them into v_price_bars_regular -- exactly the
    corruption the view exists to prevent. The only source of truth is
    tradingPeriods' start/end range.
    """

    frame: pd.DataFrame
    trading_periods: pd.DataFrame | None
    interval: str
    # Unrecoverable gap reported by plan_windows; normalize writes it to
    # bar_gaps as 'retention_expired'.
    gap: tuple[datetime, datetime] | None = None
    # Slices whose request failed. Written as 'fetch_failed' since the
    # window may still be open, and the planner retries them next run.
    failed_windows: tuple[tuple[date, date], ...] = ()
    # Successfully fetched slices. Open gaps that fall inside them get
    # closed (resolved_at); otherwise a gap written once stays open forever
    # and gets re-fetched uselessly every run.
    fetched_windows: tuple[tuple[date, date], ...] = ()
    # Open gaps read from the DB this run; normalize uses these to compute
    # which ones just closed.
    open_gaps: tuple[tuple[datetime, datetime], ...] = ()


UPDATE_COLUMNS = (
    "local_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_extended",
)

_COLUMN_MAP: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


def _session_bounds(periods: pd.DataFrame | None) -> dict[date, tuple[pd.Timestamp, pd.Timestamp]]:
    """day -> (regular session start, end).

    Only `start`/`end` are read. `pre_*`/`post_*` columns are not used, for
    two reasons: (1) they can be degenerate -- THYAO shows pre=09:30-09:30
    and post=18:00-18:00 while reg=09:30-18:00 is sound; (2) they are absent
    entirely when the call used prepost=False, and code accessing them
    would raise KeyError.
    """
    if periods is None or periods.empty:
        return {}
    if "start" not in periods.columns or "end" not in periods.columns:
        return {}
    bounds: dict[date, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for index, row in zip(periods.index, periods.to_dict("records"), strict=True):
        day = nz.to_local_date(index)
        start_ts, end_ts = row.get("start"), row.get("end")
        if day is None or nz.is_missing(start_ts) or nz.is_missing(end_ts):
            continue
        bounds[day] = (pd.Timestamp(start_ts), pd.Timestamp(end_ts))
    return bounds


def is_extended_bar(
    ts: pd.Timestamp,
    local_day: date,
    bounds: dict[date, tuple[pd.Timestamp, pd.Timestamp]],
    interval: str,
) -> bool:
    """Whether a bar falls outside the regular session.

    Four steps:
      1. interval is not intraday (1wk/1mo)   -> False (concept is meaningless)
      2. that day's session bounds are unknown -> False (safe default)
      3. ts < start or ts >= end                -> True
      4. otherwise                              -> False

    Step 2's default is deliberately False: counting an unknown bar as
    extended would hide it from v_price_bars_regular; the opposite mistake
    (counting extended as regular) is more visible and gets caught in audit.
    """
    if interval not in INTRADAY_INTERVALS:
        return False
    window = bounds.get(local_day)
    if window is None:
        return False
    start_ts, end_ts = window
    return bool(ts < start_ts or ts >= end_ts)


def normalize_bars(raw: BarPayload, symbol: str) -> NormalizedResult:
    """Converts the raw frame into price_bars rows."""
    if raw.interval not in BAR_LIMITS:
        raise ValueError(f"unknown interval: {raw.interval}")
    if nz.is_empty_result(raw.frame):
        # No early return: gap records are independent of the frame. If a
        # slice failed and no bars came back at all, that gap must still be
        # written; returning early would swallow it silently.
        return NormalizedResult(writes=_gap_writes(raw, symbol))

    frame = raw.frame
    table_name = bars_table_for(raw.interval)
    bounds = _session_bounds(raw.trading_periods)
    # Column set varies by symbol (ETFs add 'Capital Gains'); do not rely
    # on a fixed order or presence. These three extra columns are not
    # written to price_bars at all -- just ignored.
    present = {src: dst for src, dst in _COLUMN_MAP.items() if src in frame.columns}

    rows: list[dict[str, Any]] = []
    for index, record in zip(frame.index, frame.to_dict("records"), strict=True):
        # local_date is not derived from UTC: for exchanges with a positive
        # offset, a tz conversion would push the date back a day. The
        # source index already carries local tz.
        local_day = nz.to_local_date(index)
        ts_utc = nz.to_datetime_utc(index)
        if local_day is None or ts_utc is None:
            continue
        close = nz.to_decimal(record.get("Close"))
        if close is None:
            continue  # close is NOT NULL; a bar without a close is meaningless

        row: dict[str, Any] = {
            "symbol": symbol,
            "bar_interval": raw.interval,
            "ts_utc": ts_utc,
            "local_date": local_day,
            "close": close,
        }
        # `is_extended` exists only on the intraday table: the concept of
        # an extended session is meaningless for a daily-or-longer bar, and
        # `periodic_bars` has no such column (models/bars.py: PeriodicBar).
        if table_name == "price_bars":
            row["is_extended"] = is_extended_bar(
                pd.Timestamp(index), local_day, bounds, raw.interval
            )
        for src, dst in present.items():
            if dst == "close":
                continue
            value = record.get(src)
            if dst == "volume":
                volume = nz.to_int(value)
                row[dst] = None if volume is None or volume < 0 else volume
            else:
                row[dst] = nz.to_decimal(value)
        rows.append(row)

    writes = []
    if rows:
        writes.append(
            TableWrite(
                table=table_name,
                rows=rows,
                key_columns=("symbol", "bar_interval", "ts_utc"),
                # `is_extended` is intraday-specific; align_rows already
                # tolerates the missing column, and `_insert_stmt`
                # intersects the update scope with the columns present.
                update_columns=UPDATE_COLUMNS,
            )
        )
    writes.extend(_gap_writes(raw, symbol))
    return NormalizedResult(writes=writes)


def _gap_writes(raw: BarPayload, symbol: str) -> list[TableWrite]:
    """bar_gaps rows: loss record, retry record, and retry closure.

    Three sources, one table:
      retention_expired  computed by the planner, data permanently lost
      fetch_failed        a slice failed; retried next run if still in window
      resolved_at         a successful slice closes an open gap inside it

    Without the last case, bar_gaps would be a one-way list: a gap written
    once would stay open forever and be re-fetched uselessly every run.
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []

    if raw.gap is not None:
        gap_start, gap_end = raw.gap
        rows.append(
            {
                "symbol": symbol,
                "bar_interval": raw.interval,
                "gap_start_utc": nz.to_datetime_utc(gap_start),
                "gap_end_utc": nz.to_datetime_utc(gap_end),
                "detected_at": now,
                "reason": GAP_RETENTION_EXPIRED,
                # Always NULL: this is a loss record, not a retryable gap.
                "resolved_at": None,
            }
        )

    for window_start, window_end in raw.failed_windows:
        rows.append(
            {
                "symbol": symbol,
                "bar_interval": raw.interval,
                "gap_start_utc": datetime.combine(window_start, datetime.min.time()),
                "gap_end_utc": datetime.combine(window_end, datetime.min.time()),
                "detected_at": now,
                "reason": GAP_FETCH_FAILED,
                "resolved_at": None,
            }
        )

    writes: list[TableWrite] = []
    if rows:
        writes.append(
            TableWrite(
                table="bar_gaps",
                rows=rows,
                key_columns=("symbol", "bar_interval", "gap_start_utc"),
                # resolved_at is not updated here: the same key may already
                # be resolved, and reopening it would re-fetch a closed gap
                # every run.
                update_columns=("gap_end_utc", "detected_at", "reason"),
            )
        )
    writes.extend(_resolve_writes(raw, symbol, now))
    return writes


def _resolve_writes(raw: BarPayload, symbol: str, now: datetime) -> list[TableWrite]:
    """Closes open gaps that fall inside successfully fetched slices.

    A targeted upsert, not `replace_scope`: scope deletion would be wrong
    here, since it would delete the same symbol's open gaps sitting in
    other ranges.
    """
    if not raw.fetched_windows or not raw.open_gaps:
        return []
    rows: list[dict[str, Any]] = []
    for gap_start, gap_end in raw.open_gaps:
        # Window bounds are also built as UTC-aware: comparing an aware and
        # a naive datetime raises TypeError.
        covered = any(
            datetime.combine(w_start, datetime.min.time(), tzinfo=UTC)
            <= nz.utc_aware(gap_start)
            < datetime.combine(w_end, datetime.min.time(), tzinfo=UTC)
            for w_start, w_end in raw.fetched_windows
        )
        if not covered:
            continue
        rows.append(
            {
                "symbol": symbol,
                "bar_interval": raw.interval,
                "gap_start_utc": nz.utc_aware(gap_start),
                "gap_end_utc": nz.utc_aware(gap_end),
                "detected_at": now,
                "reason": GAP_FETCH_FAILED,
                "resolved_at": now,
            }
        )
    if not rows:
        return []
    return [
        TableWrite(
            table="bar_gaps",
            rows=rows,
            key_columns=("symbol", "bar_interval", "gap_start_utc"),
            update_columns=("resolved_at",),
        )
    ]


class IntervalBarDataset(Dataset[BarPayload]):
    """Writes one interval to price_bars. Name: bars_<interval>.

    Writing six classes for six intervals would be six copies of the same
    body; interval is a sampling parameter instead.

    `name` is therefore an instance attribute rather than a class attribute
    like in the base class. The Registrable protocol only requires
    `name: str`, so both are valid and the contract does not narrow.
    """

    # `produces` is an instance attribute: which table it writes to depends
    # on the interval (intraday -> price_bars, 1wk/1mo -> periodic_bars). As
    # a class attribute, auditing would report the wrong table and the
    # rescale hook (runner.py) would also fire on daily-or-longer runs.
    produces: tuple[str, ...] = ("price_bars", "bar_gaps")
    # Just "symbols". Used to be ("symbols", "splits"), which was wrong for
    # two reasons:
    #   1. "splits" is a table name, not a dataset name -- the registry
    #      resolves dependencies by dataset name, so resolution would fail.
    #   2. Even as a dataset name, SplitsDataset._SeriesDataset.fetch calls
    #      fetch_history_frame, i.e. a full 1d history() call. Even
    #      `--datasets bars_1m` would fetch 1d data. The project deliberately
    #      avoids this: corporate_actions.py has the same reason for not
    #      declaring `depends_on = ("history",)`.
    # The rescale hook works by reading the `splits` table, not by dataset
    # dependency.
    depends_on = ("symbols",)
    # Range passes through to the yfinance call -> a real backfill.
    date_range = "api"

    def __init__(self, interval: str) -> None:
        if interval not in BAR_INTERVALS:
            raise ValueError(f"unknown interval: {interval}")
        self.interval = interval
        self.name = f"bars_{interval}"
        self.produces = (bars_table_for(interval), "bar_gaps")

    def fetch(self, ctx: SyncContext) -> BarPayload:
        # Scope gate before the network call: not even one request is made
        # for a symbol out of scope.
        if not ctx.in_scope(self.interval):
            raise DatasetOutOfScope(self.interval)

        watermark = ctx.watermark(
            bars_table_for(self.interval), "ts_utc", where={"bar_interval": self.interval}
        )
        open_gaps = ctx.open_gaps(self.interval)
        plan = plan_windows(
            self.interval,
            _as_datetime(watermark),
            ctx.fetched_at,
            start=ctx.start,
            end=ctx.end,
            overlap_days=get_settings().yf_bar_overlap_days,
            open_gaps=open_gaps,
        )

        windows: list[tuple[date, date] | None] = list(plan.windows)
        if not plan.windows and plan.gap is None:
            # First fill of an unbounded interval (1wk/1mo): one call,
            # period="max"
            windows.append(None)

        # Per-slice error isolation. Treating the whole fetch as one atomic
        # unit would lose the entire 29-day first fill on a single network
        # error. A failed slice is written to bar_gaps and retried next run
        # if its window is still open.
        frames: list[pd.DataFrame] = []
        fetched: list[tuple[date, date]] = []
        failed: list[tuple[date, date]] = []
        errors: list[Exception] = []
        for window in windows:
            try:
                frames.append(self._fetch_window(ctx, window))
            except Exception as exc:  # noqa: BLE001 - per-slice error boundary
                errors.append(exc)
                if window is not None:
                    failed.append(window)
                log.warning(
                    "bar slice dropped",
                    symbol=ctx.symbol,
                    interval=self.interval,
                    window=str(window),
                    error=str(exc),
                )
                continue
            if window is not None:
                fetched.append(window)

        if errors and not frames:
            if all(is_no_data(exc) for exc in errors):
                # Every slice was answered, and the answer was "nothing
                # here": a symbol Yahoo holds no bars of at this interval
                # (measured: dozens of .IS / .KS names every run). That is
                # an EMPTY result, and recording it as FAILED run after run
                # hid the real failures in the audit. The windows still go
                # to bar_gaps as fetch_failed so a later run retries them.
                log.info(
                    "bars: yahoo holds no data",
                    symbol=ctx.symbol,
                    interval=self.interval,
                    windows=len(failed),
                )
            else:
                # No slice came back and at least one failed for a real
                # reason: returning empty silently would conflate `empty`
                # with `failed` and break proxy health accounting.
                raise errors[0]

        non_empty = [f for f in frames if not nz.is_empty_result(f)]
        frame = pd.concat(non_empty) if non_empty else pd.DataFrame()
        if not frame.empty:
            # Overlapping slices can return the same bar twice; upsert
            # tolerates this already, but avoiding the extra rows is cheaper.
            frame = frame[~frame.index.duplicated(keep="last")]

        return BarPayload(
            frame=frame,
            trading_periods=self._trading_periods(ctx),
            interval=self.interval,
            gap=plan.gap,
            failed_windows=tuple(failed),
            fetched_windows=tuple(fetched),
            open_gaps=tuple(open_gaps),
        )

    def _fetch_window(self, ctx: SyncContext, window: tuple[date, date] | None) -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "interval": self.interval,
            "auto_adjust": False,
            "actions": True,
            # Extended-hours bars are written and flagged with is_extended:
            # a missed intraday bar can never be re-fetched.
            "prepost": get_settings().yf_bar_prepost,
            # repair is off: it issues 1->6 requests for 5m, 1->8 for 15m,
            # and resamples 1wk/1mo from 1d, shifting row keys.
            "repair": False,
        }
        if window is None:
            kwargs["period"] = "max"
        else:
            # Localised HERE, not by yfinance: it localises a date string
            # with the strict default and raises on a day the exchange's
            # clock skips (measured: 2024-09-08 00:00 does not exist in
            # America/Santiago, and every 60m fill of that range failed).
            tz = _ticker_tz(ctx.ticker)
            kwargs["start"] = _local_bound(window[0], tz)
            kwargs["end"] = _local_bound(window[1], tz)
        what = f"bars_{self.interval}:{ctx.symbol}"
        return call_yahoo(lambda: ctx.ticker.history(**kwargs), what=what)

    def _trading_periods(self, ctx: SyncContext) -> pd.DataFrame | None:
        """Reads metadata after the history() call.

        Not an extra network request: history() already populates the
        metadata, and the history_metadata dataset shares the same cache.
        """
        metadata = ctx.ticker.get_history_metadata()
        periods = nz.as_mapping(metadata).get("tradingPeriods")
        return periods if isinstance(periods, pd.DataFrame) else None

    def normalize(self, raw: BarPayload, symbol: str) -> NormalizedResult:
        return normalize_bars(raw, symbol)


def _ticker_tz(ticker: Any) -> str | None:
    """The exchange's zone, from yfinance's own per-symbol cache -- the
    same lookup `history()` makes first, so it costs no extra request.
    None when the ticker cannot say; the bound then goes as a date string
    and yfinance localises it as before."""
    lookup = getattr(ticker, "_get_ticker_tz", None)
    if lookup is None:
        return None
    try:
        zone = lookup(timeout=10)
    except Exception:  # noqa: BLE001 - a missing zone must not fail the fetch
        return None
    return zone if isinstance(zone, str) and zone else None


def _local_bound(day: date, tz: str | None) -> pd.Timestamp | str:
    """Midnight of `day` on the exchange's clock, stepping over a DST
    gap instead of raising in it. yfinance converts an aware timestamp
    rather than localising it, so this is the only place the zone is
    applied."""
    if tz is None:
        return day.isoformat()
    naive = pd.Timestamp(datetime.combine(day, time.min))
    return naive.tz_localize(tz, nonexistent="shift_forward", ambiguous=True)


def _as_datetime(value: date | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


for _interval in BAR_INTERVALS:
    register(IntervalBarDataset(_interval))
