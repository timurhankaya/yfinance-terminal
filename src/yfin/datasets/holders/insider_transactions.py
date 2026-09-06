"""insider_transactions dataset'i (AH S6.3).

AS-OF DEGILDIR: kaynak islem tarihini veriyor. Saf upsert.

BIREBIR TEKILLESTIRME ZORUNLUDUR. `fact_hash` tek basina yetmez: PFE'de
DOKUZ KOLONUN TAMAMINDA ozdes iki satir olculdu (BOSHOFF CHRISTOFFEL, 8741
hisse, 263716 deger, 2025-02-21) ve hash'leri de ozdes. Tekillestirme
olmasaydi 34 satir okunup 33 yazilir, `rows_verified != rows_attempted`
her calistirmada yanlis `failed` uretirdi (S8.5). F S8.3'un `earnings_dates`
kuralinin aynisi.

Kaynak sinir ZAMAN DEGIL 150 SATIRDIR: 24 sembolun 12'sinde tam 150 satir
geldi ve pencere WMT'de 12,4 aya iniyor. `--start 2024-01-01` bu veriyi
geriye uzatmaz (AH S4.5).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import blank_to_none, in_range, key_value, to_big_value
from yfin.datasets.payloads import RangedFramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

TABLE = "insider_transactions"
KEY_COLUMNS = ("symbol", "start_date", "fact_hash")
DATA_COLUMNS = (
    "insider",
    "position",
    "text",
    "transaction_label",
    "url",
    "shares",
    "value",
    "ownership",
)
MAPPED_SOURCES = frozenset(
    {
        "Start Date",
        "Insider",
        "Position",
        "URL",
        "Transaction",
        "Text",
        "Shares",
        "Value",
        "Ownership",
    }
)
# fact_hash'e giren alanlar; `start_date` PK'da ayri bir bilesendir ve
# `fetched_at` her calistirmada degisir.
HASH_FIELDS = ("insider", "position", "text", "shares", "value", "ownership")


class InsiderTransactionsDataset(Dataset[RangedFramePayload]):
    name = "insider_transactions"
    depends_on = ("symbols",)
    produces = (TABLE,)
    date_range = "filter"

    def fetch(self, ctx: SyncContext) -> RangedFramePayload:
        frame = call_optional(
            ctx.ticker.get_insider_transactions, what=f"{self.name}:{ctx.symbol}"
        )
        return RangedFramePayload(
            frame=frame, fetched_at=ctx.fetched_at, start=ctx.start, end=ctx.end
        )

    def normalize(self, raw: RangedFramePayload, symbol: str) -> NormalizedResult:
        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        unmapped = sorted(str(c) for c in frame.columns if str(c) not in MAPPED_SOURCES)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        deduped = frame.drop_duplicates()
        dropped = len(frame) - len(deduped)
        if dropped:
            log.warning("dropped duplicate insider rows", symbol=symbol, rows=dropped)

        ranged = raw.start is not None or raw.end is not None
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for _, record in deduped.iterrows():
            start_date = nz.to_local_date(record.get("Start Date"))
            if start_date is None:
                log.warning("insider transaction has no start date", symbol=symbol)
                continue
            if ranged and not in_range(start_date, raw.start, raw.end):
                continue

            values: dict[str, Any] = {
                "insider": nz.to_str(record.get("Insider"), max_len=255),
                # '' -> NULL: BP.L'de bos Position olculdu
                "position": blank_to_none(record.get("Position"), max_len=64),
                "text": blank_to_none(record.get("Text"), max_len=255),
                "transaction_label": blank_to_none(record.get("Transaction"), max_len=64),
                "url": blank_to_none(record.get("URL")),
                "shares": to_big_value(record.get("Shares")),
                # DIS ve BP.L'de TUM satirlarda NaN
                "value": to_big_value(record.get("Value")),
                # 'D', 'I' ve 'D/I' (XOM)
                # `fact_hash`e girer, yani PK bilesenidir -> KIRPILMAZ.
                # 'D', 'I' ve 'D/I' (XOM); daha uzun bir deger gelirse
                # kirpma iki FARKLI sahiplik tipini tek hash'te birlestirir.
                "ownership": key_value(
                    record.get("Ownership"),
                    8,
                    field="ownership",
                    dataset=self.name,
                    symbol=symbol,
                )
                if record.get("Ownership") not in (None, "")
                else None,
            }
            # Hash PYTHON tarafinda HAM dizeden hesaplanir, yani 'Sale' ve
            # 'sale' AYRI iki satirdir. Bu BILINCLIDIR ve motor
            # degisiminden ETKILENMEZ: PK bileseni `fact_hash`tir ve
            # karsilastirma zaten burada, Python'da yapiliyordu.
            #
            # MySQL'de DB collation'i (utf8mb4_0900_ai_ci) ikisini AYNI
            # gorurdu ama hash zaten ayristiriyordu; PostgreSQL'de kolon
            # COLLATE "C" oldugu icin sema da ayni sonuca varir.
            # Normalizasyon EKLENMEZ -- eklenseydi bugun ayri sayilan iki
            # olay tek satira inerdi (PG S2.5.3).
            digest = nz.content_hash(
                {
                    name: (str(values[name]) if values[name] is not None else None)
                    for name in HASH_FIELDS
                }
            )
            row = {
                "symbol": symbol,
                "start_date": start_date,
                "fact_hash": digest[:16],
                **values,
                "fetched_at": raw.fetched_at,
            }
            rows[(start_date, row["fact_hash"])] = row

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=(*DATA_COLUMNS, "fetched_at"),
                )
            ]
        )


register(InsiderTransactionsDataset())
