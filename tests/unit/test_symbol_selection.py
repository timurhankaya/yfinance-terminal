"""`yfin sync` sembol secimi ve tarih araligi (AH S6.4, S7.3).

CLI'nin DOGRULAMA yollari veritabanina dokunmaz: SQLAlchemy engine tembeldir ve
bu kontroller ilk sorgudan once caliisir.
"""

from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from yfin.cli import _csv_upper, _selector, app
from yfin.datasets import SYMBOL_DATASETS

runner = CliRunner()


# --- saf yardimcilar -------------------------------------------------------


def test_csv_upper_trims_and_uppercases() -> None:
    assert _csv_upper(" nms , nyq ,, ist ") == ["NMS", "NYQ", "IST"]
    assert _csv_upper(None) == []


def test_selector_records_universe_and_range() -> None:
    """`scope` yalnizca symbols/market ayrimini tasiyor; hangi run'in hangi
    evreni kapsadigi aksi halde geriye donuk BILINEMEZ (AH S5.6)."""
    selector = _selector(
        exchange=["IST"],
        quote_type=["EQUITY"],
        suffix=None,
        start=date(2020, 1, 1),
        end=None,
    )
    assert selector == "exchange=IST quote_type=EQUITY start=2020-01-01"


def test_selector_is_none_when_unfiltered() -> None:
    """Filtresiz run'da NULL kalir; anlami "filtresiz"tir."""
    assert _selector(exchange=[], quote_type=[], suffix=None, start=None, end=None) is None


def test_selector_fits_varchar_255() -> None:
    selector = _selector(
        exchange=[f"EX{i}" for i in range(200)],
        quote_type=[],
        suffix=None,
        start=None,
        end=None,
    )
    assert selector is not None
    assert len(selector) <= 255


# --- dogrulama kurallari ---------------------------------------------------


def test_symbols_and_filters_cannot_be_combined() -> None:
    """Ikisi birden verilseydi hangisinin kazandigi SESSIZ bir varsayim
    olurdu."""
    result = runner.invoke(app, ["sync", "--symbols", "AAPL", "--exchange", "IST"])
    assert result.exit_code == 1
    assert "birlikte kullanilamaz" in result.output


def test_start_after_end_is_rejected() -> None:
    result = runner.invoke(app, ["sync", "--start", "2026-01-01", "--end", "2025-01-01"])
    assert result.exit_code == 1
    assert "--start --end'den sonra olamaz" in result.output


def test_invalid_date_is_rejected_instead_of_silently_ignored() -> None:
    """Sessizce None'a dusseydi kullanici "aralik uygulandi" sanirdi."""
    result = runner.invoke(app, ["sync", "--start", "01/01/2020"])
    assert result.exit_code == 1
    assert "gecersiz tarih" in result.output


def test_range_with_only_none_datasets_is_rejected() -> None:
    """Secilen dataset'lerin TAMAMI `date_range="none"` ise aralik hicbir
    seye etki etmezdi; sessiz no-op yerine hata verilir (AH S7.3)."""
    result = runner.invoke(app, ["sync", "--datasets", "info,fast_info", "--start", "2020-01-01"])
    assert result.exit_code == 1
    assert "tarih araligi desteklemiyor" in result.output


# --- date_range sozlesmesi -------------------------------------------------


def test_shared_history_frame_datasets_are_api_ranged() -> None:
    """Ucu de PAYLASILAN onarilmis cerceveden beslenir; aralik o CAGRIYA
    gecer, satir elemesi degildir."""
    for name in ("history", "dividends", "splits", "capital_gains", "shares_full"):
        assert SYMBOL_DATASETS[name].date_range == "api", name


def test_analysis_datasets_are_not_api_ranged() -> None:
    """Yahoo bu uclarda tarih parametresi SUNMUYOR: 17 metodun imzasi
    `as_dict` disinda parametre almiyor (base.py:210-372)."""
    for name in ("recommendations", "eps_trend", "major_holders", "funds_data"):
        assert SYMBOL_DATASETS[name].date_range != "api", name
