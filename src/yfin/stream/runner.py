"""Wires the stream process together and runs it.

The supervisor owns the event loop and the writer owns the database; this
owns neither, and exists so the CLI does not have to. What it decides:

  * that only one stream process runs at a time (advisory lock);
  * that the writer thread dying stops the process rather than letting it
    idle while dropping everything;
  * that a stop signal drains the queue before exiting.

The last two are the same concern from opposite ends -- the process must
never be up and silently collecting nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine

from yfin.core.config import Settings
from yfin.core.logging_setup import get_logger
from yfin.models.stream import StreamStatus
from yfin.storage.db import advisory_lock, session_factory
from yfin.stream.repository import STREAM_LOCK_NAME, StreamRepository
from yfin.stream.supervisor import StreamSupervisor, SupervisorConfig
from yfin.stream.writer import StreamWriter, WriterConfig

log = get_logger(__name__)

EXIT_OK = 0
EXIT_WRITER_FAILED = 3


class StreamDisabled(RuntimeError):
    """`yf_stream_enabled` is off."""


def supervisor_config(settings: Settings) -> SupervisorConfig:
    return SupervisorConfig(
        max_connections=settings.yf_stream_max_connections,
        max_symbols_per_connection=settings.yf_stream_max_symbols_per_connection,
        queue_maxsize=settings.yf_stream_queue_maxsize,
        idle_timeout_seconds=float(settings.yf_stream_idle_timeout_seconds),
        reconnect_max_seconds=settings.yf_stream_reconnect_max_seconds,
        rescan_seconds=float(settings.yf_stream_rescan_seconds),
        canary_symbols=canary_symbols(settings),
    )


def canary_symbols(settings: Settings) -> tuple[str, ...]:
    """The 24/7 symbols appended to every subscription.

    They occupy quota like anything else, which is why the per-connection
    size is 95 rather than 100.
    """
    return tuple(
        symbol.strip().upper()
        for symbol in settings.yf_stream_canary_symbols.split(",")
        if symbol.strip()
    )


def writer_config(settings: Settings) -> WriterConfig:
    return WriterConfig(
        batch_size=settings.yf_stream_batch_size,
        batch_interval_ms=settings.yf_stream_batch_interval_ms,
        quotes_every_n_batches=settings.yf_stream_quotes_every_n_batches,
        reject_sample_per_hour=settings.yf_stream_reject_sample_per_hour,
        symbol_cache_seconds=float(settings.yf_stream_rescan_seconds),
        kafka_enabled=settings.yf_kafka_enabled,
    )


@dataclass
class StreamRun:
    """What a finished run reports back."""

    exit_code: int
    rows_written: int
    session_id: int | None


@contextmanager
def _install_signal_handlers(stop: threading.Event) -> Iterator[None]:
    """SIGTERM and SIGINT set the stop event rather than killing the loop.

    A hard exit here would abandon whatever is in the queue. Handlers are
    restored on the way out so this stays usable from a test.
    """
    previous: dict[signal.Signals, object] = {}

    def handle(signum: int, _frame: object) -> None:
        log.info("stream stop requested", signal=signum)
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        # Raises off the main thread; there the caller drives `stop` itself.
        with contextlib.suppress(ValueError):
            previous[signum] = signal.signal(signum, handle)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


async def _watch(
    supervisor: StreamSupervisor, writer: StreamWriter, stop: threading.Event
) -> bool:
    """Stops the supervisor on a stop signal or a dead writer.

    Returns True when the writer was the reason. A dead writer is not
    recoverable in place: the queue fills, every tick is dropped, and the
    process keeps looking healthy -- so it ends the run instead.
    """
    while True:
        # The supervisor opens the session row (it knows the connection
        # count); the writer needs its id to flush counters onto. The
        # writer thread starts first, so the id is handed over here as
        # soon as it exists -- without this the session ends up reporting
        # zero messages and zero rows no matter what was written.
        if writer._session_id is None and supervisor._session_id is not None:
            writer.set_session(supervisor._session_id)
        if stop.is_set():
            supervisor.stop()
            return False
        if writer.failed is not None:
            log.error("writer thread died; stopping the stream")
            supervisor.stop()
            return True
        await asyncio.sleep(0.1)


async def _run_async(
    supervisor: StreamSupervisor, writer: StreamWriter, stop: threading.Event
) -> bool:
    watcher = asyncio.create_task(_watch(supervisor, writer, stop))
    try:
        await supervisor.run()
    finally:
        watcher.cancel()
    return watcher.result() if watcher.done() and not watcher.cancelled() else (
        writer.failed is not None
    )


def run_stream(
    engine: Engine,
    settings: Settings,
    *,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> StreamRun:
    """Runs the stream until stopped. Returns an exit code.

    Holds `yfin_stream` for the duration. That is a different lock from
    `yfin_sync` on purpose: the two write different tables and may run at
    the same time. `yfin stream reconcile` is the exception and takes the
    sync lock, because it writes price_bars.
    """
    if not settings.yf_stream_enabled:
        raise StreamDisabled(
            "yf_stream_enabled is off; enable it with `yfin config set yf_stream_enabled true`"
        )

    stop = stop or threading.Event()
    factory = session_factory(engine)
    repository = StreamRepository(factory)

    with advisory_lock(engine, STREAM_LOCK_NAME):
        # A previous process killed hard leaves its session `running`
        # forever; without this the table fills with sessions that look
        # live and nothing distinguishes this one from three crashed ones.
        stale = repository.close_stale_sessions()
        if stale:
            log.warning("closed sessions left running by a previous process", count=stale)

        supervisor = StreamSupervisor(repository, config=supervisor_config(settings))
        writer = StreamWriter(
            supervisor, repository, factory, config=writer_config(settings)
        )
        thread = threading.Thread(target=writer.run, name="yfin-stream-writer", daemon=True)
        thread.start()

        writer_failed = False
        try:
            with _install_signal_handlers(stop) if install_signals else _null_context():
                writer_failed = asyncio.run(_run_async(supervisor, writer, stop))
        finally:
            # Order matters: the supervisor has already stopped producing,
            # so the writer can drain what is left before the thread ends.
            writer.stop()
            thread.join(timeout=30)
            if supervisor._session_id is not None:
                repository.close_session(
                    supervisor._session_id,
                    status=StreamStatus.FAILED if writer_failed else StreamStatus.OK,
                )

    return StreamRun(
        exit_code=EXIT_WRITER_FAILED if writer_failed else EXIT_OK,
        rows_written=writer.rows_written,
        session_id=supervisor._session_id,
    )


@contextmanager
def _null_context() -> Iterator[None]:
    yield


def build_repository(engine: Engine) -> StreamRepository:
    """For the read-only CLI commands, which need no lock."""
    return StreamRepository(session_factory(engine))

