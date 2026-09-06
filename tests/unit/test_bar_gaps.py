"""bar_gaps'in UC akisi (PB S8.4).

Bunlar denetimden gecerken fark edildi: tablo yaziliyordu ama yalniz
'retention_expired' icin. Iki akis eksikti ve eksiklikleri sessizdi:

  * `fetch_failed` HIC YAZILMIYORDU -> planlayicinin open_gaps mekanizmasi
    hicbir zaman dolmayan bir tablodan okuyordu, yani OLU KODDU.
  * `resolved_at` HIC DOLDURULMUYORDU -> bir kez yazilan bosluk sonsuza
    kadar acik kalir ve HER kosuda bosuna yeniden cekilirdi.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from yfin.datasets.bars import BarPayload, normalize_bars


def _payload(**kwargs: Any) -> BarPayload:
    defaults: dict[str, Any] = {
        "frame": pd.DataFrame(),
        "trading_periods": None,
        "interval": "1m",
    }
    defaults.update(kwargs)
    return BarPayload(**defaults)


def _gap_rows(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for write in result.writes:
        if write.table == "bar_gaps":
            rows.extend(write.rows)
    return rows


def test_retention_gap_is_recorded_as_a_permanent_loss() -> None:
    payload = _payload(gap=(datetime(2026, 8, 1), datetime(2026, 8, 6)))

    rows = _gap_rows(normalize_bars(payload, "AAPL"))

    assert len(rows) == 1
    assert rows[0]["reason"] == "retention_expired"
    # KAYIP KAYDIDIR, gorev degil: yeniden denenmemeli
    assert rows[0]["resolved_at"] is None


def test_failed_window_is_recorded_as_a_retryable_task() -> None:
    """Pencere hala acik olabilir; planlayici bunu yeniden dener."""
    payload = _payload(failed_windows=((date(2026, 9, 1), date(2026, 9, 3)),))

    rows = _gap_rows(normalize_bars(payload, "AAPL"))

    assert len(rows) == 1
    assert rows[0]["reason"] == "fetch_failed"
    assert rows[0]["resolved_at"] is None
    assert rows[0]["gap_start_utc"] == datetime(2026, 9, 1)


def test_successful_window_closes_the_open_gap_inside_it() -> None:
    """Bu olmadan bosluk sonsuza kadar acik kalirdi."""
    payload = _payload(
        fetched_windows=((date(2026, 9, 1), date(2026, 9, 5)),),
        open_gaps=((datetime(2026, 9, 2), datetime(2026, 9, 3)),),
    )

    rows = _gap_rows(normalize_bars(payload, "AAPL"))

    assert len(rows) == 1
    assert rows[0]["resolved_at"] is not None


def test_open_gap_outside_the_fetched_window_stays_open() -> None:
    payload = _payload(
        fetched_windows=((date(2026, 9, 1), date(2026, 9, 5)),),
        open_gaps=((datetime(2026, 8, 20), datetime(2026, 8, 21)),),
    )

    assert _gap_rows(normalize_bars(payload, "AAPL")) == []


def test_resolution_write_only_touches_resolved_at() -> None:
    """Kapatma yazimi `reason`i guncellemez: ayni satirin gerekcesini
    degistirmek denetim izini bozardi."""
    payload = _payload(
        fetched_windows=((date(2026, 9, 1), date(2026, 9, 5)),),
        open_gaps=((datetime(2026, 9, 2), datetime(2026, 9, 3)),),
    )

    write = next(w for w in normalize_bars(payload, "AAPL").writes if w.table == "bar_gaps")

    assert write.update_columns == ("resolved_at",)


def test_recording_write_never_reopens_a_resolved_gap() -> None:
    """`fetch_failed` yazimi resolved_at'i GUNCELLEMEZ: kapanmis bir
    boslugu yeniden acmak onu her kosuda tekrar cektirirdi."""
    payload = _payload(failed_windows=((date(2026, 9, 1), date(2026, 9, 3)),))

    write = next(w for w in normalize_bars(payload, "AAPL").writes if w.table == "bar_gaps")

    assert "resolved_at" not in write.update_columns


def test_no_gap_no_write() -> None:
    assert normalize_bars(_payload(), "AAPL").writes == []
