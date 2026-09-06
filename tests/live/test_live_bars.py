"""price_bars canli entegrasyon testleri (PB S9.3).

`-m live`: gercek Yahoo API + gercek MySQL. Fixture testleri kaynagin
DUNKU seklini dogrular; bunlar BUGUNKU seklini dogrular ve
BAR_LIMITS'in sapmasini erken yakalar.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from yfin.client import make_ticker
from yfin.datasets.bars import BAR_LIMITS, IntervalBarDataset, plan_windows
from yfin.datasets.base import SyncContext
from yfin.persistence import MySQLRowWriter

pytestmark = pytest.mark.live


def _ctx(symbol: str) -> SyncContext:
    return SyncContext(
        symbol,
        make_ticker(symbol),
        datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.mark.parametrize("symbol", ["AAPL", "THYAO.IS"])
def test_end_to_end_5m_write(db_session: Session, symbol: str) -> None:
    """fetch -> normalize -> yaz -> dogrula."""
    # ON DUPLICATE KEY SART: live testler AYNI semayi paylasir ve daha
    # once kosan bir modul (test_live_analysis) ayni sembolu COMMIT etmis
    # olabilir. Ciplak INSERT o durumda duplicate key ile duser - tek
    # basina kosarken gecen, birlikte kosarken kirilan bir test uretir.
    db_session.execute(
        text(
            "INSERT INTO symbols (symbol, is_active, unknown_streak, created_at, updated_at) "
            "VALUES (:s, 1, 0, NOW(6), NOW(6)) ON DUPLICATE KEY UPDATE symbol = symbol"
        ),
        {"s": symbol},
    )
    db_session.flush()

    dataset = IntervalBarDataset("5m")
    payload = dataset.fetch(_ctx(symbol))
    result = dataset.normalize(payload, symbol)
    stats = dataset.upsert(MySQLRowWriter(db_session), result)

    assert stats.attempted["price_bars"] > 0
    assert stats.verified["price_bars"] == stats.attempted["price_bars"]


def test_first_fill_1m_slices_stay_inside_the_request_limit() -> None:
    """Planlayicinin dilimleri GERCEK sinirda hata ALMAMALI.

    Bu testin varlik sebebi olculmus bir hatadir: BAR_LIMITS["1m"]
    onceden (8, 30) idi ve ilk dilim tam sinirda basladigi icin canli
    kosuda YFPricesMissingError aldi. Yahoo sinirlari degistirirse burasi
    once kirilir.
    """
    now = datetime.now(UTC)
    plan = plan_windows("1m", None, now)
    ticker = make_ticker("MSFT")

    oldest = plan.windows[0]
    frame = ticker.history(
        interval="1m", start=oldest[0].isoformat(), end=oldest[1].isoformat(), prepost=True
    )

    assert not frame.empty, f"en eski dilim ({oldest[0]} -> {oldest[1]}) reddedildi"


@pytest.mark.parametrize("interval", ["5m", "60m"])
def test_declared_depth_is_still_accepted_by_yahoo(interval: str) -> None:
    """BAR_LIMITS'teki derinlik degerleri hala gecerli mi.

    Bir gun Yahoo pencereyi daraltirsa bu test, veri sessizce kaybolmaya
    baslamadan ONCE kirilir.
    """
    _per_request, depth = BAR_LIMITS[interval]
    assert depth is not None
    now = datetime.now(UTC)
    plan = plan_windows(interval, None, now)
    oldest = plan.windows[0]

    frame = make_ticker("MSFT").history(
        interval=interval, start=oldest[0].isoformat(), end=oldest[1].isoformat()
    )

    assert not frame.empty


def test_weekly_uses_period_max_and_returns_monday_anchored_bars() -> None:
    """1wk ilk dolumu dilimsizdir (period='max') ve barlar Pazartesi
    hizalidir (PB S4.7)."""
    now = datetime.now(UTC)
    plan = plan_windows("1wk", None, now)
    assert plan.windows == ()

    payload = IntervalBarDataset("1wk").fetch(_ctx("MSFT"))

    assert not payload.frame.empty
    assert all(ts.weekday() == 0 for ts in payload.frame.index[:20])


def test_extended_flag_is_set_for_a_us_symbol() -> None:
    """AAPL prepost'lu cekimde seans disi bar URETMELI; uretmiyorsa ya
    prepost kapanmis ya tradingPeriods sekli degismistir."""
    dataset = IntervalBarDataset("5m")
    payload = dataset.fetch(_ctx("AAPL"))
    rows = dataset.normalize(payload, "AAPL").writes[0].rows

    assert any(r["is_extended"] for r in rows)
    assert any(not r["is_extended"] for r in rows)
