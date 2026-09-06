"""sustainability IZLEME dataset'i (AH S6.3).

Tablosu YOKTUR (`produces = ()`): `esgScores` modulu 19 sembolun 19'unda da
404 dondu -- 8 sektor, 6 ulke (AAPL, MSFT, KO, XOM, TSLA, JPM, NVDA, GE,
PFE, WMT, BA, INTC, DIS, F, THYAO.IS, NESN.SW, BP.L, 005930.KS, BABA). Hic
dolmayacak bir tablo acmak yerine ucun geri acilip acilmadigini IZLEYEN bir
dataset kalir; kaynak dolu gelirse WARNING ile haber verir.

`all` GENISLEMESINDE YOKTUR (`opt_in=True`): kosturulursa her sembolde bir
bosa istek ve bir `empty` hucre uretir. Kayit KOSULSUZDUR ama opt-in'dir --
dataset `--datasets sustainability` ile ADIYLA istendiginde kosar.
"""

from __future__ import annotations

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)


class SustainabilityDataset(Dataset[AsOfFramePayload]):
    name = "sustainability"
    depends_on = ("symbols",)
    # HICBIR tabloya yazmaz. `runner._record_items` bu bosluga karsi
    # `or dataset.produces or [None]` ile korunur (AH S6.5/1); aksi halde
    # dataset denetimden butunuyle kaybolurdu.
    produces = ()

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(ctx.ticker.get_sustainability, what=f"{self.name}:{ctx.symbol}")
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if not nz.is_empty_result(frame):
            assert frame is not None
            log.warning(
                "sustainability now returns data",
                symbol=symbol,
                shape=str(getattr(frame, "shape", None)),
                columns=[str(c) for c in getattr(frame, "columns", [])],
            )
        # Dolu gelse de YAZILMAZ: tablo yok. Terfi karari olcumle verilir.
        return NormalizedResult()


# KOSULSUZ KAYIT, `opt_in=True` ILE (CFG S3.4).
#
# Onceki hali `if get_settings().yf_probe_sustainability:` idi ve bu, kod
# tabanindaki TEK modul-govdesi `get_settings()` cagrisiydi. Zincir
# `cli.py -> yfin.datasets -> analysis -> sustainability` oldugu icin
# `yfin --help` bile Settings'i kurmaya zorluyordu; DB katmani
# eklendiginde bu, DB'ye hic dokunmayan komutlarin DB'ye baglanmasi ve DB
# kapaliyken KURTARMA komutlarinin bile calismamasi demek olurdu.
#
# `yf_discovery_enabled` ayni tuzaga dusmus ve kaldirilmisti
# (config.py'deki not); `sustainability` o gocte atlanmisti. Davranis
# ayni kalir: dataset `--datasets sustainability` ile ADIYLA istendiginde
# kosar, `all` genislemesinde HIC gorunmez. `yf_probe_sustainability`
# alani ise TAMAMEN KALDIRILDI: bu degisiklikten sonra onu okuyan kimse
# kalmiyordu ve etkisiz bir ayar, bir yonetim panelini besleyen
# yapilandirma katmaninda yanlis bilgi demektir (config.py'deki not).
register(SustainabilityDataset(), opt_in=True)
