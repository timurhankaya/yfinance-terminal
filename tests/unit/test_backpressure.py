"""Verifies the queue limit is actually binding.

Collecting results on the main thread via future.result() gives no such
guarantee: pool.submit() accepts every symbol immediately, and completed
payloads pile up inside Future objects. The tests below show the queue
actually blocks the worker.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _run(strategy: str, symbols: int, maxsize: int, workers: int) -> int:
    """Measures how many symbols get processed while the consumer never consumes."""
    results: queue.Queue[str] = queue.Queue(maxsize=maxsize)
    processed = 0
    lock = threading.Lock()
    done = threading.Event()

    def work(symbol: str) -> str:
        nonlocal processed
        with lock:
            processed += 1
        return symbol

    def produce_bounded() -> None:
        def run_one(symbol: str) -> None:
            results.put(work(symbol))  # blocks here if the queue is full

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_one, [f"S{i}" for i in range(symbols)]))
        done.set()

    def produce_unbounded() -> None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, f"S{i}") for i in range(symbols)]
            for future in futures:
                results.put(future.result())
        done.set()

    target = produce_bounded if strategy == "bounded" else produce_unbounded
    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    # The consumer is deliberately idle; workers must stall once the queue fills
    time.sleep(0.4)
    with lock:
        measured = processed

    # Release blocked workers, otherwise the pool never shuts down
    while not done.is_set():
        try:
            results.get(timeout=0.05)
        except queue.Empty:
            continue
    thread.join(timeout=10)
    assert not thread.is_alive()
    return measured


def test_bounded_strategy_stops_at_queue_limit() -> None:
    """When workers enqueue themselves, production stalls at
    maxsize + worker count."""
    maxsize, workers, symbols = 8, 4, 200
    processed = _run("bounded", symbols, maxsize, workers)
    assert processed <= maxsize + workers + 1, processed
    assert processed < symbols


def test_unbounded_strategy_processes_everything() -> None:
    """The old approach: every symbol gets processed even with a full
    queue, and normalized data piles up in memory."""
    maxsize, workers, symbols = 8, 4, 200
    processed = _run("unbounded", symbols, maxsize, workers)
    assert processed > maxsize + workers + 1, processed
