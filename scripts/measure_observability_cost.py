"""What observing the pipeline costs the pipeline.

Three numbers the design asks for, and all three are about the same
question: does the instrumentation change the thing it measures? A log
format that costs a millisecond a line is a different format on a run that
writes a million of them; a span per dataset is 46 spans per symbol; a
scrape that renders the whole registry holds the GIL while the stream's
writer thread wants it.

    uv run python scripts/measure_observability_cost.py

Needs no database, no network and no collector. The numbers go into
docs/measurements/observability.md.
"""

from __future__ import annotations

import argparse
import io
import logging
import time
from collections.abc import Callable

from yfin.core import metrics, tracing
from yfin.core.logging_setup import configure_logging, get_logger

#: Enough repetitions that the timer's resolution is not the measurement,
#: few enough that the whole script stays under a minute.
LINES = 20_000
SPANS = 20_000
SCRAPES = 200


def _timed(work: Callable[[], None], repeat: int) -> float:
    """Seconds per operation, taking the best of three runs.

    The best rather than the mean: what is being measured is the cost of
    the work, and a run that was slower because something else on the
    machine wanted the CPU measures the machine, not the code.
    """
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best / repeat


def _silence() -> io.StringIO:
    """A root handler that formats fully and writes nowhere.

    Writing to /dev/null would still pay for the syscall, and writing to a
    terminal would measure the terminal. The formatter runs in full either
    way, which is the part under test.
    """
    sink = io.StringIO()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setStream(sink)  # type: ignore[attr-defined]
    return sink


def measure_logging() -> tuple[float, float]:
    print("## Log rendering, per line\n")
    log = get_logger("measurement")

    results = {}
    for fmt in ("console", "json"):
        configure_logging("INFO", fmt, "sync")
        _silence()

        def emit() -> None:
            for index in range(LINES):
                log.info(
                    "dataset written",
                    dataset="history",
                    symbol_count=index,
                    rows=index * 3,
                    duration_ms=12.5,
                )

        results[fmt] = _timed(emit, LINES)

    # A line that is DROPPED by the level filter, which is what a DEBUG
    # call costs in a process running at INFO. `filter_by_level` is first
    # in the chain precisely so this stays near zero.
    configure_logging("INFO", "json", "sync")
    _silence()

    def dropped() -> None:
        for index in range(LINES):
            log.debug("not emitted", index=index)

    below = _timed(dropped, LINES)

    for fmt, seconds in results.items():
        print(f"| `{fmt}` | {seconds * 1e6:.1f} us |")
    print(f"| below the level | {below * 1e6:.2f} us |")
    print()
    print(f"json / console: {results['json'] / results['console']:.2f}x")
    print()
    return results["console"], results["json"]


def measure_tracing() -> None:
    print("## Spans, per span\n")

    tracing._reset_for_tests()

    def off() -> None:
        for _ in range(SPANS):
            with tracing.span("sync.symbol", symbol="AAPL", dataset_count=46):
                pass

    disabled = _timed(off, SPANS)

    sdk = None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        sdk = True
    except ImportError:
        print("(the `[otel]` extra is not installed; only the off case)")

    enabled = None
    if sdk:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
        trace._TRACER_PROVIDER = provider  # noqa: SLF001
        tracing._enabled = True  # noqa: SLF001

        def on() -> None:
            for _ in range(SPANS):
                with tracing.span("sync.symbol", symbol="AAPL", dataset_count=46):
                    pass

        enabled = _timed(on, SPANS)
        tracing._reset_for_tests()

    print(f"| off (`_enabled` false) | {disabled * 1e6:.3f} us |")
    if enabled is not None:
        print(f"| on, sampling 1.0 | {enabled * 1e6:.2f} us |")
        # One `sync.symbol` plus one `sync.dataset.fetch` per dataset.
        per_symbol = enabled * 47
        print()
        print(f"per symbol (1 + 46 spans): {per_symbol * 1e3:.2f} ms")
    print()


def measure_scrape() -> None:
    print("## One scrape, and the GIL\n")
    from prometheus_client import generate_latest

    # Fill the registry the way a running service does: the scheduler
    # holds every exporter gauge, and a real universe is 49 datasets across
    # three scopes.
    for scope in ("symbols", "market", "domain"):
        for index in range(49):
            metrics.set_gauge(
                "yfin_cells_total", 100, scope=scope, dataset=f"dataset_{index}"
            )
            metrics.set_gauge(
                "yfin_cells_stale", 3, scope=scope, dataset=f"dataset_{index}"
            )
    for state in ("healthy", "unknown", "cooldown", "dead", "disabled"):
        metrics.set_gauge("yfin_proxies", 4, state=state)

    def scrape() -> None:
        for _ in range(SCRAPES):
            generate_latest()

    seconds = _timed(scrape, SCRAPES)
    series = len(generate_latest().splitlines())
    print(f"| exposition lines | {series} |")
    print(f"| per scrape | {seconds * 1e3:.2f} ms |")
    print()
    print(
        f"at a 15 s scrape interval: {seconds / 15 * 100:.4f} % of wall clock "
        "with the GIL held"
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lines-per-sync",
        type=int,
        default=0,
        help="Log lines one sync writes; turns the per-line cost into a "
        "per-run one. Count them with `docker logs yfin-backfill | wc -l`.",
    )
    args = parser.parse_args()

    console, json_line = measure_logging()
    measure_tracing()
    measure_scrape()

    if args.lines_per_sync:
        print("## Over one full sync\n")
        for name, seconds in (("console", console), ("json", json_line)):
            total = seconds * args.lines_per_sync
            print(f"| `{name}` | {total:.1f} s |")
        print()
        print(
            f"difference over {args.lines_per_sync:,} lines: "
            f"{abs(json_line - console) * args.lines_per_sync:.1f} s"
        )


if __name__ == "__main__":
    main()
