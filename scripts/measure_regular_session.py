"""WebSocket measurement during the regular US session: message rate, field
fill rates and whether `market_hours` distinguishes a session.

Run it inside the session, preferably ~30 minutes after the open. Decodes via
`protocol.decode_envelope`; writes nothing. Usage: [--minutes 10] [--json out.json]"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yfin.stream.connection import StreamConnection  # noqa: E402
from yfin.stream.protocol import (  # noqa: E402
    BIGINT_FIELDS,
    DOUBLE_FIELDS,
    FIELD_COLUMNS,
    MARKET_HOURS_NAMES,
    PRICE_FIELDS,
    QUOTE_TYPE_NAMES,
    TEXT_FIELDS,
)
from yfin.stream.rejects import DecodeResult  # noqa: E402
from yfin.stream.topology import ConnectionPlan  # noqa: E402

#: The same 13 symbols as round 2, so the two are directly comparable.
#: Ten liquid US equities across sectors plus three 24/7 crypto pairs; the
#: crypto side is the control -- if it goes quiet too, the problem is the
#: connection, not the market.
EQUITIES: tuple[str, ...] = (
    "AAPL", "NVDA", "TSLA", "MSFT", "META", "SPY", "QQQ", "AMD", "AMZN", "GOOGL",
)
CRYPTO: tuple[str, ...] = ("BTC-USD", "ETH-USD", "SOL-USD")

#: Columns worth a fill-rate line. Derived from the protocol's own field
#: tables rather than retyped, so a new upstream field shows up here
#: without anyone remembering to add it.
TRACKED: tuple[str, ...] = (
    *PRICE_FIELDS,
    *BIGINT_FIELDS,
    *DOUBLE_FIELDS,
    *TEXT_FIELDS,
    "price_hint",
    "quote_type",
    "market_hours",
)


class Collector:
    """Counts what arrived, per symbol group. Holds no rows."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.filled: dict[str, Counter[str]] = defaultdict(Counter)
        self.market_hours: dict[str, Counter[int]] = defaultdict(Counter)
        self.quote_types: dict[str, Counter[int]] = defaultdict(Counter)
        self.lags: list[float] = []
        self.gaps: dict[str, list[float]] = defaultdict(list)
        self.rejects: Counter[str] = Counter()
        self._last_seen: dict[str, datetime] = {}
        self.started = datetime.now(UTC)

    def on_result(self, result: DecodeResult) -> None:
        for reject in result.rejects:
            self.rejects[reject.reason] += 1
        row = result.row
        if row is None:
            return
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return

        self.counts[symbol] += 1
        group = "crypto" if symbol in CRYPTO else "equity"

        for field in TRACKED:
            column = FIELD_COLUMNS.get(field, field)
            if row.get(column) is not None:
                self.filled[group][field] += 1

        if (code := row.get("market_hours_code")) is not None:
            self.market_hours[group][int(code)] += 1
        if (code := row.get("quote_type_code")) is not None:
            self.quote_types[group][int(code)] += 1

        ts = row.get("ts_utc")
        received = row.get("received_at")
        if isinstance(ts, datetime) and isinstance(received, datetime):
            self.lags.append((received - ts).total_seconds())
        if isinstance(ts, datetime):
            if previous := self._last_seen.get(symbol):
                gap = (ts - previous).total_seconds()
                # Yahoo re-sends the same snapshot on reconnect; a zero or
                # negative gap is not an interval between two updates.
                if gap > 0:
                    self.gaps[symbol].append(gap)
            self._last_seen[symbol] = ts

    # --- reporting ---------------------------------------------------------

    def group_total(self, group: str) -> int:
        members = CRYPTO if group == "crypto" else EQUITIES
        return sum(self.counts[s] for s in members)

    def report(self, minutes: float) -> dict[str, Any]:
        return {
            "measured_at": self.started.isoformat(),
            "minutes": minutes,
            "messages": dict(self.counts),
            "totals": {g: self.group_total(g) for g in ("equity", "crypto")},
            "fill_rates": {
                group: {
                    field: round(self.filled[group][field] / total, 4)
                    for field in TRACKED
                }
                for group in ("equity", "crypto")
                if (total := self.group_total(group))
            },
            "market_hours": {
                g: {MARKET_HOURS_NAMES.get(k, str(k)): v for k, v in c.items()}
                for g, c in self.market_hours.items()
            },
            "quote_types": {
                g: {QUOTE_TYPE_NAMES.get(k, str(k)): v for k, v in c.items()}
                for g, c in self.quote_types.items()
            },
            "lag_seconds": _percentiles(self.lags),
            "median_gap_seconds": {
                s: round(statistics.median(v), 2) for s, v in sorted(self.gaps.items()) if v
            },
            "rejects": dict(self.rejects),
        }


def _percentiles(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    def at(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 2)
    return {"n": len(ordered), "p50": at(0.50), "p95": at(0.95), "p99": at(0.99)}


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    minutes = report["minutes"]
    equity, crypto = report["totals"]["equity"], report["totals"]["crypto"]
    lines.append(f"# Regular-session round -- {report['measured_at']} ({minutes:g} min)\n")

    if not crypto:
        lines.append("!! No crypto messages either: this is a CONNECTION failure,")
        lines.append("   not a market observation. Do not record this round.\n")
    elif not equity:
        lines.append("!! Crypto flowed but no equity did. Check the market really was")
        lines.append("   open -- that is exactly how the Labor Day round went wrong.\n")

    lines.append(f"equities {equity} message(s), crypto {crypto}")
    if equity:
        lines.append(
            f"equity rate: {equity / len(EQUITIES) / minutes:.2f} msg/symbol/min"
        )
    lines.append("")

    lines.append("## Messages per symbol\n")
    for symbol in (*EQUITIES, *CRYPTO):
        gap = report["median_gap_seconds"].get(symbol)
        suffix = f"   median gap {gap}s" if gap else ""
        lines.append(f"  {symbol:<10} {report['messages'].get(symbol, 0):>5}{suffix}")

    lines.append("\n## Field fill rates\n")
    rates = report["fill_rates"]
    lines.append(f"  {'field':<22} {'equity':>8} {'crypto':>8}")
    for field in TRACKED:
        e = f"{rates.get('equity', {}).get(field, 0) * 100:.0f}%" if equity else "-"
        c = f"{rates.get('crypto', {}).get(field, 0) * 100:.0f}%" if crypto else "-"
        lines.append(f"  {field:<22} {e:>8} {c:>8}")

    lines.append("\n## market_hours (the open question)\n")
    for group, counts in report["market_hours"].items():
        lines.append(f"  {group}: {counts}")
    lines.append(
        "\n  Round 2 saw regular_market on a closed market. If equities read\n"
        "  regular_market here too, the field never distinguishes a session."
    )

    lines.append("\n## Lag (received_at - ts_utc)\n")
    lines.append(f"  {report['lag_seconds']}")
    if report["rejects"]:
        lines.append(f"\n## Rejects\n\n  {report['rejects']}")
    return "\n".join(lines)


async def _collect(minutes: float) -> Collector:
    collector = Collector()
    plan = ConnectionPlan(
        key="measure", exchange="mixed", symbols=(*EQUITIES, *CRYPTO)
    )
    # No canary: 13 symbols is far below the 100 truncation point, so the
    # instrument would only add a symbol to reason about.
    connection = StreamConnection(plan, on_result=collector.on_result)
    task = asyncio.ensure_future(connection.run())
    try:
        await asyncio.sleep(minutes * 60)
    finally:
        connection.stop()
        await asyncio.wait_for(task, timeout=30)
    return collector


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--json", type=Path, help="also write the raw report here")
    args = parser.parse_args()

    now = datetime.now(UTC)
    print(f"starting {now.isoformat()} -- listening for {args.minutes:g} minute(s)")
    if not (13 <= now.hour < 20):
        print(
            "WARNING: outside 13:30-20:00 UTC. The US regular session is what\n"
            "         this script exists to measure; anything else repeats the\n"
            "         mistake of the first two rounds.",
            file=sys.stderr,
        )

    collector = asyncio.run(_collect(args.minutes))
    report = collector.report(args.minutes)
    print()
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nraw report -> {args.json}")
    return 0 if report["totals"]["crypto"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
