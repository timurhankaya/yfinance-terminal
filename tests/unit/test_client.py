"""Rate limiting ve retry davranisi (S7.5)."""

from __future__ import annotations

import threading
import time

import pytest

from yfin.ingest.client import TokenBucket, is_retryable


class TestTokenBucket:
    def test_rate_is_enforced(self) -> None:
        bucket = TokenBucket(rate_per_sec=20.0, burst=1.0)
        bucket.acquire()
        started = time.monotonic()
        for _ in range(4):
            bucket.acquire()
        elapsed = time.monotonic() - started
        # 4 token @ 20/sn ~= 0.2 sn; zamanlama toleransli olcuulur
        assert elapsed >= 0.15, elapsed

    def test_burst_allows_initial_calls(self) -> None:
        bucket = TokenBucket(rate_per_sec=1.0, burst=5.0)
        started = time.monotonic()
        for _ in range(5):
            bucket.acquire()
        assert time.monotonic() - started < 0.1

    def test_thread_safe_under_contention(self) -> None:
        """Limiter process-global ve KILITLIDIR (S7.1)."""
        bucket = TokenBucket(rate_per_sec=1000.0, burst=1000.0)
        counter = {"n": 0}
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                bucket.acquire()
                with lock:
                    counter["n"] += 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert counter["n"] == 400


class TestRetryClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("429 Client Error: Too Many Requests for url: https://query2..."),
            RuntimeError("503 Server Error: Service Unavailable for url"),
            RuntimeError("HTTP 503"),
            RuntimeError("500 Internal Server Error"),
            RuntimeError("Read timed out"),
            TimeoutError("x"),
            ConnectionError("x"),
        ],
    )
    def test_retryable(self, exc: BaseException) -> None:
        assert is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("currency"),
            ValueError("gecersiz sembol"),
            TypeError("x"),
            # Durum kodu gibi gorunen ama HTTP olmayan mesajlar
            ValueError("Symbol 500 not found"),
            ValueError("no data for 5000 rows"),
            ValueError("$NOSUCH: No data found, symbol may be delisted"),
        ],
    )
    def test_not_retryable(self, exc: BaseException) -> None:
        assert is_retryable(exc) is False


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testte exponential backoff beklemesi anlamsizdir."""
    from yfin.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "yf_retry_initial_sec", 0.0)
    monkeypatch.setattr(settings, "yf_retry_max_sec", 0.0)


class TestCallYahoo:
    def test_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from yfin.ingest import client

        monkeypatch.setattr(client, "_bucket", TokenBucket(1000.0, 1000.0))
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("429 Client Error: Too Many Requests for url")
            return "ok"

        assert client.call_yahoo(flaky, what="test") == "ok"
        assert attempts["n"] == 3

    def test_non_retryable_raises_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from yfin.ingest import client

        monkeypatch.setattr(client, "_bucket", TokenBucket(1000.0, 1000.0))
        attempts = {"n": 0}

        def broken() -> str:
            attempts["n"] += 1
            raise KeyError("currency")

        with pytest.raises(KeyError):
            client.call_yahoo(broken, what="test")
        assert attempts["n"] == 1

    def test_retries_pass_through_the_limiter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tenacity retry'lari da limiter'dan gecer (S7.5)."""
        from yfin.ingest import client

        calls = {"n": 0}

        class CountingBucket(TokenBucket):
            def acquire(self, tokens: float = 1.0) -> None:
                calls["n"] += 1

        monkeypatch.setattr(client, "_bucket", CountingBucket(1000.0, 1000.0))
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("503 Server Error: Service Unavailable for url")
            return "ok"

        client.call_yahoo(flaky, what="test")
        assert calls["n"] == 3
