"""`yfin sync` symbol selection and date range.

The CLI's validation paths never touch a database: the SQLAlchemy engine
is lazy, and these checks run before the first query.
"""

from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from yfin.cli import common
from yfin.cli.app import app
from yfin.datasets import SYMBOL_DATASETS

runner = CliRunner()


# --- pure helpers ------------------------------------------------------------


def test_comma_list_trims_uppercases_and_drops_empties() -> None:
    """The dropped-empty is the part that matters: a trailing comma in a
    hand-edited `.env` used to reach a region loop as `""`."""
    from yfin.core.text import comma_list

    assert comma_list(" nms , nyq ,, ist ", upper=True) == ["NMS", "NYQ", "IST"]
    assert comma_list(" a , b ") == ["a", "b"]
    assert comma_list(None) == []
    assert comma_list("") == []
    assert comma_list(" , , ") == []


def test_selector_records_universe_and_range() -> None:
    """`scope` only carries the symbols/market distinction; otherwise which
    universe a given run covered could never be known retroactively."""
    selector = common.selector(
        exchange=["IST"],
        quote_type=["EQUITY"],
        suffix=None,
        start=date(2020, 1, 1),
        end=None,
    )
    assert selector == "exchange=IST quote_type=EQUITY start=2020-01-01"


def test_selector_is_none_when_unfiltered() -> None:
    """Stays NULL for an unfiltered run; that means "no filter"."""
    assert common.selector(exchange=[], quote_type=[], suffix=None, start=None, end=None) is None


def test_selector_fits_varchar_255() -> None:
    selector = common.selector(
        exchange=[f"EX{i}" for i in range(200)],
        quote_type=[],
        suffix=None,
        start=None,
        end=None,
    )
    assert selector is not None
    assert len(selector) <= 255


# --- validation rules ---------------------------------------------------


def test_symbols_and_filters_cannot_be_combined() -> None:
    """If both were given, which one wins would be a silent assumption."""
    result = runner.invoke(app, ["sync", "--symbols", "AAPL", "--exchange", "IST"])
    assert result.exit_code == 1
    assert "cannot be combined with" in result.output


def test_start_after_end_is_rejected() -> None:
    result = runner.invoke(app, ["sync", "--start", "2026-01-01", "--end", "2025-01-01"])
    assert result.exit_code == 1
    assert "--start cannot be later than --end" in result.output


def test_invalid_date_is_rejected_instead_of_silently_ignored() -> None:
    """Silently falling back to None would make the user think a range was applied."""
    result = runner.invoke(app, ["sync", "--start", "01/01/2020"])
    assert result.exit_code == 1
    assert "invalid date for --start" in result.output


def test_range_with_only_none_datasets_is_rejected() -> None:
    """If every selected dataset has `date_range="none"`, the range would
    affect nothing; this raises an error instead of a silent no-op."""
    result = runner.invoke(app, ["sync", "--datasets", "info,fast_info", "--start", "2020-01-01"])
    assert result.exit_code == 1
    assert "supports a date range" in result.output


# --- date_range contract ---------------------------------------------------


def test_shared_history_frame_datasets_are_api_ranged() -> None:
    """All five feed from the shared repaired frame; the range goes into
    that call, not a row filter."""
    for name in ("history", "dividends", "splits", "capital_gains", "shares_full"):
        assert SYMBOL_DATASETS[name].date_range == "api", name


def test_analysis_datasets_are_not_api_ranged() -> None:
    """Yahoo offers no date parameter for these endpoints: 17 methods'
    signatures take no parameter beyond `as_dict` (base.py:210-372)."""
    for name in ("recommendations", "eps_trend", "major_holders", "funds_data"):
        assert SYMBOL_DATASETS[name].date_range != "api", name
