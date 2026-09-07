"""The Kafka extra has to stay optional, and only a test keeps it that way.

CI installs `dev` and `api`, not `kafka`. So every one of these passes
today by accident: a single top-level `import confluent_kafka` anywhere in
`yfin.stream` would break `yfin stream run` for everyone who never asked
for Kafka, and the failure would land at import time on a data-pipeline
host that has no use for a broker.

The import is deferred into `build_producer` / `existing_topics`
deliberately, and that placement is what these tests pin.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from yfin.outbox.kafka import KafkaUnavailable

STREAM_MODULES = [
    "yfin.stream.protocol",
    "yfin.stream.topology",
    "yfin.stream.connection",
    "yfin.stream.repository",
    "yfin.stream.supervisor",
    "yfin.stream.writer",
    "yfin.outbox.kafka",
    "yfin.outbox.relay",
    "yfin.stream.reconcile",
    "yfin.stream.runner",
    "yfin.cli.stream",
]


def _is_stream_module(name: str) -> bool:
    return name.startswith("yfin.stream") or name == "yfin.cli.stream"


@contextmanager
def _without_confluent_kafka() -> Iterator[None]:
    """Makes `import confluent_kafka` fail, the way a plain install does.

    Also restores every `yfin.stream` module afterwards. These tests have
    to re-import those modules to observe import-time behaviour, and a
    re-imported module is a DIFFERENT object: an exception class defined
    in it no longer matches the one another test imported at collection
    time, so `pytest.raises(StreamDisabled)` stops matching and unrelated
    tests fail. Found exactly that way -- two runner tests broke as soon
    as this file joined the suite.
    """
    real_import = builtins.__import__
    hidden = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "confluent_kafka"
        or name.startswith("confluent_kafka.")
        or _is_stream_module(name)
    }

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "confluent_kafka" or name.startswith("confluent_kafka."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    for name in list(sys.modules):
        if name == "confluent_kafka" or name.startswith("confluent_kafka."):
            del sys.modules[name]
    builtins.__import__ = guarded
    try:
        yield
    finally:
        builtins.__import__ = real_import
        for name in list(sys.modules):
            if _is_stream_module(name):
                del sys.modules[name]
        sys.modules.update(hidden)


@pytest.mark.parametrize("module_name", STREAM_MODULES)
def test_stream_modules_import_without_the_kafka_extra(module_name: str) -> None:
    """A data-pipeline host installs no broker client and must still run."""
    with _without_confluent_kafka():
        for name in list(sys.modules):
            if _is_stream_module(name):
                del sys.modules[name]
        importlib.import_module(module_name)


def test_building_a_producer_without_the_extra_fails_clearly() -> None:
    """Loud at the point of use, not silent.

    A stream that thinks it is publishing and is not would leave the
    operator waiting for messages that never come.
    """
    with _without_confluent_kafka():
        for name in list(sys.modules):
            if _is_stream_module(name):
                del sys.modules[name]
        kafka = importlib.import_module("yfin.outbox.kafka")
        with pytest.raises(kafka.KafkaUnavailable, match="not installed"):
            kafka.build_producer("localhost:9092", client_id="x")


def test_the_error_names_the_extra_to_install() -> None:
    with _without_confluent_kafka():
        for name in list(sys.modules):
            if _is_stream_module(name):
                del sys.modules[name]
        kafka = importlib.import_module("yfin.outbox.kafka")
        with pytest.raises(kafka.KafkaUnavailable, match=r'yfin\[kafka\]'):
            kafka.build_producer("localhost:9092", client_id="x")


def test_kafka_unavailable_is_importable_either_way() -> None:
    """Callers catch this without needing the extra themselves."""
    assert issubclass(KafkaUnavailable, RuntimeError)


def test_no_stream_module_imports_confluent_kafka_at_module_level() -> None:
    """Reads the source rather than relying on import order.

    The deferred import is easy to 'tidy up' to the top of the file
    during an unrelated change; this says out loud that it is placed
    where it is on purpose.
    """
    import inspect

    for module_name in STREAM_MODULES:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import confluent_kafka", "from confluent_kafka")):
                assert line.startswith((" ", "\t")), (
                    f"{module_name} imports confluent_kafka at module level; "
                    f"the extra is optional and CI does not install it"
                )
