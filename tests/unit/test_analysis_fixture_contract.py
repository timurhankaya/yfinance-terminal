"""GERCEK yakalanmis fixture'lara karsi kaynak sozlesmesi (AH S9.1).

`test_analysis.py` / `test_holders.py` / `test_funds.py` elle kurulmus
cerceveler kullanir ve normalizasyon MANTIGINI dogrular. Bu dosya farkli bir
seyi dogrular: kodun bekledigi KAYNAK ANAHTARLARI ve SEKILLERI Yahoo'nun
gercekten dondurdugu seyle esitmi.

Ayrim onemlidir. Sentetik bir cerceve yalnizca YAZARIN INANDIGINI kodlar:
`downLast7days` hem koda hem teste kucuk `d` ile yazilsaydi test GECER ve
kolon sonsuza kadar sessizce NULL kalirdi. Buradaki iddialar yakalanmis
gercek govdeye bakar, bu yuzden Yahoo bir anahtari degistirdiginde ya da kod
sapmaya basladiginda KIRILIRLAR.

Fixture yoksa testler atlanir (`helpers.load_fixture` -> pytest.skip).
"""

from __future__ import annotations

from typing import Any

from helpers import load_fixture

# --- kaynak anahtar adlari -------------------------------------------------


def _columns(records: Any) -> set[str]:
    """Yakalanmis cerceve kayitlarindaki kolon adlari."""
    if not records:
        return set()
    return {k for k in records[0] if k != "index"}


def test_eps_revisions_key_really_has_capital_d() -> None:
    """AH S4.2'nin en sinsi bulgusu: uc anahtar kucuk `d` ile biterken
    `downLast7Days` BUYUK D ile gelir. Dokumantasyon dordunu de kucuk
    yaziyor."""
    for symbol in ("AAPL", "MSFT", "KO"):
        cols = _columns(load_fixture(symbol, "eps_revisions"))
        if not cols:
            continue
        assert "downLast7Days" in cols, f"{symbol}: {sorted(cols)}"
        assert "downLast7days" not in cols
        assert {"upLast7days", "upLast30days", "downLast30days"} <= cols


def test_estimate_frames_carry_undocumented_currency_column() -> None:
    """`currency` dokumantasyonda YOK ama dort estimate cercevesinde de var."""
    for dataset in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
        cols = _columns(load_fixture("AAPL", dataset))
        assert "currency" in cols, f"{dataset}: {sorted(cols)}"


def test_upgrades_downgrades_has_seven_columns_not_four() -> None:
    """Dokumantasyon 4 kolon yaziyor; kaynak 7 donduruyor."""
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
    """Dokumantasyon `+5y`/`-5y` yaziyor; kaynak `LTG` donduruyor."""
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
    """Iki dataset'in TEK tabloya yazmasinin gerekcesi (AH S5.2)."""
    inst = _columns(load_fixture("AAPL", "institutional_holders"))
    fund = _columns(load_fixture("AAPL", "mutualfund_holders"))
    assert inst == fund
    assert {"Date Reported", "Holder", "pctHeld", "Shares", "Value", "pctChange"} == inst


def test_insider_purchases_first_column_carries_the_period() -> None:
    """0. kolon ADI dinamiktir; etiket ADDAN degil KONUMDAN okunmalidir."""
    records = load_fixture("AAPL", "insider_purchases")
    cols = [k for k in records[0] if k != "index"]
    header = cols[0]
    assert header.startswith("Insider Purchases Last "), header
    assert {"Shares", "Trans"} <= set(cols)


# --- sembole ozgu kenar durumlari -----------------------------------------


def test_pfe_really_has_two_fully_identical_insider_rows() -> None:
    """`fact_hash`in TEK BASINA yetmedigi kanit. Bu satirlar ayrisamaz;
    normalize birebir tekillestirme yapmak ZORUNDADIR (AH S8.3)."""
    records = load_fixture("PFE", "insider_transactions")
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    for row in records:
        key = tuple(sorted((k, str(v)) for k, v in row.items() if k != "index"))
        if key in seen:
            duplicates += 1
        seen.add(key)
    assert duplicates >= 1, "PFE'de birebir ozdes satir bekleniyordu"


def test_xom_ownership_has_a_three_character_value() -> None:
    """`AsciiKeyType(2)` bu degeri kirpardi."""
    records = load_fixture("XOM", "insider_transactions")
    values = {str(r.get("Ownership")) for r in records}
    assert "D/I" in values, sorted(values)


def test_nvda_insider_roster_has_the_two_extra_columns() -> None:
    """Kolon seti sembole gore 7/9/11'dir. positionSummary kolona
    alinmasaydi NVDA'da bir kisinin TEK hisse bilgisi kaybolurdu."""
    cols = _columns(load_fixture("NVDA", "insider_roster_holders"))
    assert "positionSummary" in cols
    assert "positionSummaryDate" in cols


def test_insider_roster_column_set_varies_across_symbols() -> None:
    """Sabit kolon setine guvenilemez -> normalize row.get(...) kullanir."""
    sizes = {
        symbol: len(_columns(load_fixture(symbol, "insider_roster_holders")))
        for symbol in ("AAPL", "NVDA")
    }
    assert len(set(sizes.values())) > 1, sizes


def test_ko_insider_purchases_has_a_negative_net_value() -> None:
    """`BigNumType` ISARETLI olmali; UNSIGNED olsaydi ERROR 1264 ile
    SEMBOLUN TUM transaction'i duserdi."""
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
