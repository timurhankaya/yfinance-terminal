"""Source contract against real captured fixtures.

`test_analysis.py` / `test_holders.py` / `test_funds.py` use hand-built
frames and verify normalization logic. This file verifies something
different: whether the source keys and shapes the code expects match what
Yahoo actually returns.

The distinction matters. A synthetic frame only encodes what the author
believes: if `downLast7days` were written with a lowercase `d` in both the
code and the test, the test would pass and the column would stay silently
NULL forever. The assertions here look at the actual captured body, so
they break when Yahoo changes a key or the code drifts.

Tests are skipped when a fixture is missing (`helpers.load_fixture` ->
pytest.skip).
"""

from __future__ import annotations

from typing import Any

from helpers import load_fixture

# --- source key names ---------------------------------------------------


def _columns(records: Any) -> set[str]:
    """Column names in a captured frame's records."""
    if not records:
        return set()
    return {k for k in records[0] if k != "index"}


def test_eps_revisions_key_really_has_capital_d() -> None:
    """The sneakiest finding: three keys end in lowercase `d`, but
    `downLast7Days` comes with a capital D. The docs write all four in
    lowercase."""
    for symbol in ("AAPL", "MSFT", "KO"):
        cols = _columns(load_fixture(symbol, "eps_revisions"))
        if not cols:
            continue
        assert "downLast7Days" in cols, f"{symbol}: {sorted(cols)}"
        assert "downLast7days" not in cols
        assert {"upLast7days", "upLast30days", "downLast30days"} <= cols


def test_estimate_frames_carry_undocumented_currency_column() -> None:
    """`currency` is not documented, but all four estimate frames have it."""
    for dataset in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
        cols = _columns(load_fixture("AAPL", dataset))
        assert "currency" in cols, f"{dataset}: {sorted(cols)}"


def test_upgrades_downgrades_has_seven_columns_not_four() -> None:
    """The docs list 4 columns; the source returns 7."""
    cols = _columns(load_fixture("AAPL", "upgrades_downgrades"))
    assert cols == {
        "Firm",
        "ToGrade",
        "FromGrade",
        "Action",
        "priceTargetAction",
        "currentPriceTarget",
        "priorPriceTarget",
    }


def test_growth_estimates_index_has_ltg_not_five_year() -> None:
    """The docs say `+5y`/`-5y`; the source returns `LTG`."""
    records = load_fixture("AAPL", "growth_estimates")
    periods = {r["index"] for r in records}
    assert "LTG" in periods
    assert not {"+5y", "-5y"} & periods
    assert _columns(records) == {"stockTrend", "indexTrend"}


def test_major_holders_has_the_four_measured_keys() -> None:
    records = load_fixture("AAPL", "major_holders")
    assert {r["index"] for r in records} == {
        "insidersPercentHeld",
        "institutionsPercentHeld",
        "institutionsFloatPercentHeld",
        "institutionsCount",
    }


def test_institutional_and_mutualfund_columns_are_identical() -> None:
    """Why the two datasets write to a single table."""
    inst = _columns(load_fixture("AAPL", "institutional_holders"))
    fund = _columns(load_fixture("AAPL", "mutualfund_holders"))
    assert inst == fund
    assert {"Date Reported", "Holder", "pctHeld", "Shares", "Value", "pctChange"} == inst


def test_insider_purchases_first_column_carries_the_period() -> None:
    """Column 0's name is dynamic; the label must be read by position, not by name."""
    records = load_fixture("AAPL", "insider_purchases")
    cols = [k for k in records[0] if k != "index"]
    header = cols[0]
    assert header.startswith("Insider Purchases Last "), header
    assert {"Shares", "Trans"} <= set(cols)


# --- symbol-specific edge cases -------------------------------------------


def test_pfe_really_has_two_fully_identical_insider_rows() -> None:
    """Proof `fact_hash` alone is not enough. These rows are
    indistinguishable; normalize must dedupe on an exact match."""
    records = load_fixture("PFE", "insider_transactions")
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    for row in records:
        key = tuple(sorted((k, str(v)) for k, v in row.items() if k != "index"))
        if key in seen:
            duplicates += 1
        seen.add(key)
    assert duplicates >= 1, "expected a fully identical row in PFE"


def test_xom_ownership_has_a_three_character_value() -> None:
    """`AsciiKeyType(2)` would truncate this value."""
    records = load_fixture("XOM", "insider_transactions")
    values = {str(r.get("Ownership")) for r in records}
    assert "D/I" in values, sorted(values)


def test_nvda_insider_roster_has_the_two_extra_columns() -> None:
    """The column set is 7/9/11 depending on the symbol. If positionSummary
    were not captured as a column, one person's only share info in NVDA
    would be lost."""
    cols = _columns(load_fixture("NVDA", "insider_roster_holders"))
    assert "positionSummary" in cols
    assert "positionSummaryDate" in cols


def test_insider_roster_column_set_varies_across_symbols() -> None:
    """A fixed column set cannot be relied on -> normalize uses row.get(...)."""
    sizes = {
        symbol: len(_columns(load_fixture(symbol, "insider_roster_holders")))
        for symbol in ("AAPL", "NVDA")
    }
    assert len(set(sizes.values())) > 1, sizes


def test_ko_insider_purchases_has_a_negative_net_value() -> None:
    """`BigNumType` must be signed; if it were UNSIGNED, ERROR 1264 would
    drop the symbol's entire transaction."""
    records = load_fixture("KO", "insider_purchases")
    shares = [r.get("Shares") for r in records if r.get("Shares") is not None]
    assert any(float(v) < 0 for v in shares), shares


def test_wmt_position_exceeds_the_originally_measured_length() -> None:
    """Ilk olcum 23 karakter demisti; gercek 56. KeyTextType(64) yeterli."""
    records = load_fixture("WMT", "insider_transactions")
    longest = max((len(str(r.get("Position") or "")) for r in records), default=0)
    assert longest > 23
    assert longest <= 64


def test_gspc_returns_nothing_for_every_analysis_dataset() -> None:
    """Endekste 16 dataset'in tamami bostur; bunlar `empty`tir, `failed`
    degil (AH S8.2)."""
    for dataset in (
        "recommendations",
        "upgrades_downgrades",
        "earnings_estimate",
        "revenue_estimate",
        "eps_trend",
        "eps_revisions",
        "earnings_history",
        "growth_estimates",
        "major_holders",
        "institutional_holders",
        "mutualfund_holders",
        "insider_purchases",
        "insider_transactions",
        "insider_roster_holders",
    ):
        assert not load_fixture("^GSPC", dataset), dataset
    assert not load_fixture("^GSPC", "analyst_price_targets")


def test_analyst_price_targets_has_five_keys() -> None:
    payload = load_fixture("AAPL", "analyst_price_targets")
    assert set(payload) == {"current", "low", "high", "mean", "median"}
