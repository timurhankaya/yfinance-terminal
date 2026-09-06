"""upgrades_downgrades dataset'i -> analyst_grade_changes (AH S6.3).

AS-OF DEGILDIR: kaynak her satirin kendi tarihini (`epochGradeDate`)
tasiyor. SAF UPSERT'tir; `replace_scope` olsaydi ~1000 satirlik kaynak
tavaninin disinda kalan eski kayitlar her calistirmada silinirdi (AH S4.5).

Resmi dokumantasyon dort kolon yaziyor (base.py:227-231); olcum YEDI buldu:
`priceTargetAction`, `currentPriceTarget`, `priorPriceTarget` de geliyor
(15 sembolde ayni).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.base import Dataset, NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import blank_to_none, in_range, key_value
from yfin.datasets.payloads import RangedFramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger

log = get_logger(__name__)

TABLE = "analyst_grade_changes"
FIRM_LENGTH = 64
KEY_COLUMNS = ("symbol", "grade_ts_utc", "firm")
UPDATE_COLUMNS = (
    "to_grade",
    "from_grade",
    "action",
    "price_target_action",
    "current_price_target",
    "prior_price_target",
    "fetched_at",
)
# Tabloya tasinan kaynak kolonlari; disinda kalan her anahtar S8.5 uyarisi
# uretir (kaynak yeni bir kolon eklerse sessizce kaybolmasin).
MAPPED_SOURCES = frozenset(
    {
        "Firm",
        "ToGrade",
        "FromGrade",
        "Action",
        "priceTargetAction",
        "currentPriceTarget",
        "priorPriceTarget",
    }
)


class UpgradesDowngradesDataset(Dataset[RangedFramePayload]):
    name = "upgrades_downgrades"
    depends_on = ("symbols",)
    produces = (TABLE,)
    # Kaynak sabit bir pencere donduruyor; aralik satir eler (AH S6.2).
    date_range = "filter"

    def fetch(self, ctx: SyncContext) -> RangedFramePayload:
        frame = call_optional(
            ctx.ticker.get_upgrades_downgrades, what=f"{self.name}:{ctx.symbol}"
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

        ranged = raw.start is not None or raw.end is not None
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}

        for index, record in frame.iterrows():
            # Kaynak tz-naive AMA epochGradeDate SANIYESINDEN uretilmis
            # (quote.py:577) -> UTC'dir; ikinci bir tz donusumu YAPILMAZ.
            ts_utc = nz.to_datetime_utc(index)
            if ts_utc is None:
                log.warning("grade change has no timestamp", symbol=symbol)
                continue
            if ranged and not in_range(ts_utc.date(), raw.start, raw.end):
                continue

            # firm PK bilesenidir: bos dize iki FARKLI kaydi tek satirda
            # birlestirirdi, kirpilmis bir ad da oyle.
            firm = key_value(
                record.get("Firm"),
                FIRM_LENGTH,
                field="firm",
                dataset=self.name,
                symbol=symbol,
            )
            if firm is None:
                continue

            row = {
                "symbol": symbol,
                "grade_ts_utc": ts_utc,
                "firm": firm,
                "to_grade": blank_to_none(record.get("ToGrade"), max_len=32),
                "from_grade": blank_to_none(record.get("FromGrade"), max_len=32),
                "action": blank_to_none(record.get("Action"), max_len=16),
                "price_target_action": blank_to_none(
                    record.get("priceTargetAction"), max_len=16
                ),
                # 0.0 GERCEK bir degerdir; NULL'a cevrilmez.
                "current_price_target": nz.to_decimal(record.get("currentPriceTarget")),
                "prior_price_target": nz.to_decimal(record.get("priorPriceTarget")),
                "fetched_at": raw.fetched_at,
            }
            rows[(ts_utc, firm)] = row

        if not rows:
            return NormalizedResult()

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=UPDATE_COLUMNS,
                )
            ]
        )


register(UpgradesDowngradesDataset())
