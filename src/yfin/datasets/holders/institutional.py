"""institutional_holders + mutualfund_holders -> institutional_holders (AH S6.3).

Iki dataset AYNI tabloya yazar; 14 sembolde kolon setleri birebir ayni
olculdu. Kapsamlari `scope_columns=(symbol, as_of_date, holder_type)` ile
ayrisir -- `scope_columns`'in var olma nedeni tam olarak budur. Ayri kayit
kalirlar cunku ayri `sync_run_items` hucresi ve ayri `--datasets`
secilebilirligi gerekir.

`scope_values` ACIKCA verilir (AH S7.2): kapsam SATIRLARDAN turetilseydi,
kaynak bir kalemi birakinca eski satir kapsamin disinda kalip kalirdi.

SINIR -- BILINCLI: kaynak BUTUNUYLE bosalirsa (bos cerceve ya da 404)
`normalize` erken doner ve `AsOfDataset.upsert` `is_empty` gorup hicbir
sey yazmaz; o gunun MEVCUT satirlari TEMIZLENMEZ. Ilk taslak bunun tersini
iddia ediyordu, ama erken donus `scope_values`'tan ONCE gelir. Davranis
KASITLI olarak boyle birakilmistir: `call_optional` gecici bir 404'u
gercek bir "sifir sahip" durumundan AYIRT EDEMEZ ve silme, gecici bir
kesintide o as-of gununun verisini yok ederdi. Bedeli, ayni gun icinde
iki kosu yapilir ve ikincisi bos donerse sabahki satirlarin o gunun
etiketiyle kalmasidir.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from yfin import normalize as nz
from yfin.client import call_optional
from yfin.datasets.asof_base import AsOfDataset, asof_produces
from yfin.datasets.base import NormalizedResult, SyncContext, TableWrite
from yfin.datasets.common import key_value
from yfin.datasets.payloads import AsOfFramePayload
from yfin.datasets.registry import register
from yfin.logging_setup import get_logger
from yfin.models.holders import HolderType

log = get_logger(__name__)

TABLE = "institutional_holders"
HOLDER_LENGTH = 128
KEY_COLUMNS = ("symbol", "as_of_date", "holder_type", "holder")
SCOPE_COLUMNS = ("symbol", "as_of_date", "holder_type")
DATA_COLUMNS = ("date_reported", "pct_held", "pct_change", "shares", "value")
MAPPED_SOURCES = frozenset({"Date Reported", "Holder", "pctHeld", "pctChange", "Shares", "Value"})


class _HolderListDataset(AsOfDataset[AsOfFramePayload]):
    depends_on = ("symbols",)
    produces = asof_produces(TABLE)
    holder_type: HolderType
    api_method: str

    def fetch(self, ctx: SyncContext) -> AsOfFramePayload:
        frame = call_optional(
            getattr(ctx.ticker, self.api_method), what=f"{self.name}:{ctx.symbol}"
        )
        return AsOfFramePayload(frame=frame, fetched_at=ctx.fetched_at)

    def normalize(self, raw: AsOfFramePayload, symbol: str) -> NormalizedResult:
        as_of = raw.fetched_at.date()
        # Kapsam degerleri satirlardan DEGIL burada uretilir: kaynak bir
        # KALEMI birakinca eski satir kapsam disinda kalmasin diye. Kaynagin
        # TAMAMEN bosalmasi ayri bir durumdur; modul docstring'indeki
        # "SINIR" notuna bakiniz.
        scope_values = (
            {"symbol": symbol, "as_of_date": as_of, "holder_type": self.holder_type.value},
        )

        frame = raw.frame
        if nz.is_empty_result(frame):
            return NormalizedResult()
        assert isinstance(frame, pd.DataFrame)

        unmapped = sorted(str(c) for c in frame.columns if str(c) not in MAPPED_SOURCES)
        if unmapped:
            log.warning("unmapped keys", dataset=self.name, symbol=symbol, keys=unmapped)

        rows: dict[str, dict[str, Any]] = {}
        for _, record in frame.iterrows():
            holder = key_value(
                record.get("Holder"),
                HOLDER_LENGTH,
                field="holder",
                dataset=self.name,
                symbol=symbol,
            )
            if holder is None:
                continue
            rows[holder] = {
                "symbol": symbol,
                "as_of_date": as_of,
                # ENUM degeri DAIMA kucuk harf uretilir; DB'nin ai_ci
                # sessiz donusumune ('INSTITUTION' -> 'institution')
                # guvenilmez (AH S5).
                "holder_type": self.holder_type.value,
                "holder": holder,
                # SATIR BAZINDA degisir: AAPL mutualfund'da tek listede
                # dort farkli tarih. Takvimsel etiket -> tz donusumu yok.
                "date_reported": nz.to_local_date(record.get("Date Reported")),
                "pct_held": nz.to_decimal(record.get("pctHeld")),
                "pct_change": nz.to_decimal(record.get("pctChange")),
                "shares": nz.to_decimal(record.get("Shares")),
                "value": nz.to_decimal(record.get("Value")),
                "fetched_at": raw.fetched_at,
            }

        return NormalizedResult(
            writes=[
                TableWrite(
                    table=TABLE,
                    rows=list(rows.values()),
                    key_columns=KEY_COLUMNS,
                    update_columns=(*DATA_COLUMNS, "fetched_at"),
                    mode="replace_scope",
                    scope_columns=SCOPE_COLUMNS,
                    scope_values=scope_values,
                )
            ]
        )


class InstitutionalHoldersDataset(_HolderListDataset):
    name = "institutional_holders"
    api_method = "get_institutional_holders"
    holder_type = HolderType.INSTITUTION


class MutualFundHoldersDataset(_HolderListDataset):
    name = "mutualfund_holders"
    api_method = "get_mutualfund_holders"
    holder_type = HolderType.MUTUALFUND


register(InstitutionalHoldersDataset())
register(MutualFundHoldersDataset())
