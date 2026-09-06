"""SQ S6.1: `scope="variant"` -- ucuncu dis dongu.

Bu dosyanin iki isi var:
1. Yeni `variant` dalinin calistigini gostermek.
2. MEVCUT `region` / `global` dallarinin BIREBIR ayni kaldigini surmek.

Ikincisi birincisinden onemlidir: alti piyasa dataset'i uretimde kosuyor ve
bu degisiklik onlarin davranisina dokunmamalidir.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from yfin.datasets.base import NormalizedResult
from yfin.datasets.market.base import GlobalDataset, MarketContext

FETCHED_AT = datetime(2026, 9, 5, 12, 0, 0)
START = date(2026, 9, 1)
END = date(2026, 10, 1)


def _ctx() -> MarketContext:
    return MarketContext(fetched_at=FETCHED_AT, start=START, end=END)


class TestClone:
    def test_for_variant_sets_variant_and_leaves_region_none(self) -> None:
        """SQ S6.1: ekran bir BOLGE DEGILDIR.

        `region`a yazilsaydi `sync_run_items.region` bolge semantigini
        kaybeder ve "hangi bolgede kostu" sorgusu ekran adlari dondururdu.
        """
        clone = _ctx().for_variant("day_gainers")
        assert clone.variant == "day_gainers"
        assert clone.region is None

    def test_for_region_does_not_drop_variant(self) -> None:
        """REGRESYON: `for_region` eskiden alanlari ELLE sayiyordu.

        `variant` eklenip orada da sayilmasaydi bolge dalinda SESSIZCE
        duserdi. `_clone` tek klonlama noktasi oldugu icin bu artik
        yapisal olarak imkansiz -- test o yapiyi kilitler.
        """
        base = _ctx().for_variant("tr_equity")
        assert base.for_region("US").variant == "tr_equity"

    def test_for_variant_does_not_drop_region(self) -> None:
        base = _ctx().for_region("EUROPE")
        assert base.for_variant("day_gainers").region == "EUROPE"

    def test_clone_preserves_window(self) -> None:
        """`start`/`end` ZORUNLU alanlardir (varsayilani yok); klonlama
        onlari tasimasaydi TypeError verirdi."""
        clone = _ctx().for_variant("x")
        assert (clone.start, clone.end, clone.fetched_at) == (START, END, FETCHED_AT)

    def test_clones_share_one_cache(self) -> None:
        """Ayni (anahtar, tur) icin ham yanit BIR KEZ cekilir."""
        base = _ctx()
        calls: list[int] = []

        def fetch() -> str:
            calls.append(1)
            return "payload"

        assert base.for_variant("a").cached("k", fetch) == "payload"
        assert base.for_variant("b").cached("k", fetch) == "payload"
        assert base.for_region("US").cached("k", fetch) == "payload"
        assert len(calls) == 1


class _RecordingDataset(GlobalDataset[None]):
    """Hangi kapsam etiketiyle kac kez cagrildigini kaydeder."""

    produces = ()

    def __init__(self, name: str, scope: str, variants: tuple[str, ...] = ()) -> None:
        self.name = name
        self.scope = scope  # type: ignore[assignment]
        self._variants = variants
        self.seen: list[tuple[str | None, str | None]] = []

    def variants(self, settings: Any, session: Any) -> tuple[str, ...]:
        return self._variants

    def fetch(self, mctx: MarketContext) -> None:
        self.seen.append((mctx.region, mctx.variant))

    def normalize(self, raw: None) -> NormalizedResult:  # pragma: no cover
        return NormalizedResult()


class TestVariantsContract:
    def test_default_variants_is_empty(self) -> None:
        """Mevcut alti dataset `variants()` TANIMLAMAZ; varsayilan bos
        olmasaydi hepsi `scope="variant"` gibi davranmaya calisirdi."""
        dataset = _RecordingDataset("market_status", "region")
        assert dataset.variants(None, None) == ()

    def test_variant_dataset_sees_each_key_once(self) -> None:
        dataset = _RecordingDataset("screener", "variant", ("day_gainers", "tr_equity"))
        base = _ctx()
        for key in dataset.variants(None, None):
            dataset.fetch(base.for_variant(key))
        assert dataset.seen == [(None, "day_gainers"), (None, "tr_equity")]

    def test_region_dataset_is_untouched(self) -> None:
        """MEVCUT davranis: bolge dalinda `variant` NULL kalir ve
        `sync_run_items.region` eskisi gibi bolgeyi tasir."""
        dataset = _RecordingDataset("market_summary", "region")
        base = _ctx()
        for region in ("US", "EUROPE"):
            dataset.fetch(base.for_region(region))
        assert dataset.seen == [("US", None), ("EUROPE", None)]

    def test_global_dataset_is_untouched(self) -> None:
        dataset = _RecordingDataset("earnings_calendar", "global")
        dataset.fetch(_ctx())
        assert dataset.seen == [(None, None)]
