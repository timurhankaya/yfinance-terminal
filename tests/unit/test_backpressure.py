"""Kuyruk sinirinin gercekten baglayici oldugunu dogrular (S7.1).

Sonuclari ana thread'de future.result() ile toplamak bu garantiyi vermez:
pool.submit() tum sembolleri aninda kabul eder ve tamamlanan payload'lar
Future nesnelerinde birikir. Asagidaki testler kuyrugun worker'i bloke
ettigini gosterir.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def _run(strategy: str, symbols: int, maxsize: int, workers: int) -> int:
    """Tuketici hic tuketmezken kac sembolun islendigini olcer."""
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
            results.put(work(symbol))  # kuyruk doluysa BURADA bloke olur

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

    # Tuketici kasitli olarak beklemede; worker'lar kuyruk dolunca durmali
    time.sleep(0.4)
    with lock:
        measured = processed

    # Bloke worker'lari serbest birak, aksi halde pool kapanmaz
    while not done.is_set():
        try:
            results.get(timeout=0.05)
        except queue.Empty:
            continue
    thread.join(timeout=10)
    assert not thread.is_alive()
    return measured


def test_bounded_strategy_stops_at_queue_limit() -> None:
    """Worker kuyruga kendisi koyunca uretim maxsize + worker sayisi
    kadarinda durur."""
    maxsize, workers, symbols = 8, 4, 200
    processed = _run("bounded", symbols, maxsize, workers)
    assert processed <= maxsize + workers + 1, processed
    assert processed < symbols


def test_unbounded_strategy_processes_everything() -> None:
    """Eski yaklasim: kuyruk dolu olsa da tum semboller islenir ve
    normalize edilmis veri bellekte birikir."""
    maxsize, workers, symbols = 8, 4, 200
    processed = _run("unbounded", symbols, maxsize, workers)
    assert processed > maxsize + workers + 1, processed
