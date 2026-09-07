"""`.env.example` is documentation that can go stale silently.

It went stale: it carried `YF_PROBE_SUSTAINABILITY=0` long after the
setting was deliberately removed (`core/config.py`, "There is no
YF_PROBE_SUSTAINABILITY key"). Copying the file and setting that value
did nothing, and nothing failed. A setup file that lies is worse than a
missing one, because the reader trusts it.

These tests read the file rather than a copy of its contents, so they
fail when the file drifts, not when someone forgets to update a fixture.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from yfin.api.core.config import ApiSettings
from yfin.core.config import Settings
from yfin.storage.settings_store import ENV_ONLY_FIELDS

ENV_EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _declared_keys() -> set[str]:
    if not ENV_EXAMPLE.exists():  # pragma: no cover - the file is committed
        pytest.skip(".env.example is not present")
    return set(ASSIGNMENT.findall(ENV_EXAMPLE.read_text()))


def _setting_names() -> set[str]:
    """Every key that reaches a settings object, from BOTH of them.

    `ApiSettings` was missing here, and the gap was not academic: all
    twelve `YFAPI_*` keys were absent from the file, `YFAPI_JWT_SIGNING_KEY`
    among them -- without which the API refuses to sign a token -- and no
    test could notice, because this function only knew about `Settings`.
    """
    prefix = ApiSettings.model_config["env_prefix"]
    return {name.upper() for name in Settings.model_fields} | {
        f"{prefix}{name}".upper() for name in ApiSettings.model_fields
    }


def test_every_key_is_a_real_setting() -> None:
    """A key nobody reads is a silent no-op for whoever sets it."""
    phantom = sorted(_declared_keys() - _setting_names())
    assert not phantom, (
        "these keys are in .env.example but are not settings, so setting "
        f"them does nothing: {', '.join(phantom)}"
    )


def test_every_env_only_setting_is_documented() -> None:
    """The env-only fields have no other way in.

    Everything else is DB-managed and reachable through `yfin config`, so
    its absence here is a choice. These eight cannot be set any other
    way, so leaving one out means an operator cannot configure it without
    reading the source.
    """
    missing = sorted({name.upper() for name in ENV_ONLY_FIELDS} - _declared_keys())
    assert not missing, f"env-only settings missing from .env.example: {', '.join(missing)}"


def test_every_api_setting_is_documented() -> None:
    """`ApiSettings` is env-only in its entirety.

    It has no `settings` table behind it and no `yfin config` command --
    that separation is the point of the class (`api/core/config.py`: an
    operator changing `yf_max_shards` has no business changing the JWT
    audience). So the file a new operator copies is the ONLY place these
    twelve can be learned from, and an omission here is not documentation
    drift, it is a key nobody can find.
    """
    prefix = ApiSettings.model_config["env_prefix"]
    expected = {f"{prefix}{name}".upper() for name in ApiSettings.model_fields}
    missing = sorted(expected - _declared_keys())
    assert not missing, f"API settings missing from .env.example: {', '.join(missing)}"


def test_the_signing_key_is_documented_as_mandatory() -> None:
    """It has a default (`""`) so that commands which never sign a token
    still run, so nothing fails at import to announce it. The API refuses
    to mint a token without it, and the file is where an operator finds
    that out before deploying rather than after."""
    text = ENV_EXAMPLE.read_text()
    assert "YFAPI_JWT_SIGNING_KEY=" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("YFAPI_JWT_SIGNING_KEY="))
    index = text.splitlines().index(line)
    comment = "\n".join(text.splitlines()[max(0, index - 6) : index]).lower()
    assert "required" in comment or "zorunlu" in comment, (
        "YFAPI_JWT_SIGNING_KEY carries no note saying it is mandatory"
    )


def test_no_mysql_left_in_the_setup_file() -> None:
    """The engine is PostgreSQL. A leftover MySQL note in the file a new
    operator copies first is the most expensive place for stale text."""
    text = ENV_EXAMPLE.read_text().lower()
    for token in ("mysql", "3306", "pymysql", "mariadb", "innodb"):
        assert token not in text, f"{token!r} still appears in .env.example"
