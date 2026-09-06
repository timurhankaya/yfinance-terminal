"""history_metadata.exchange_timezone_name (PB S6.6/2)

Eksik alan. Mevcut `timezone` kolonu IANA ADI DEGILDIR: olculdu, AAPL'de
"EDT", THYAO.IS'te "TRT" doner - DST'ye bagli kisaltmalar, ZoneInfo'ya
verilemez. IANA adi (America/New_York, Europe/Istanbul) kaynakta
`exchangeTimezoneName` alanindadir ve history_metadata dataset'i onu
bugune kadar yalniz raw_json'a birakiyordu (warn_unmapped uyarisiyla).

rescale, split gununu sembolun YEREL 00:00'ina hizalamak zorundadir; ham
UTC gece yarisi alinirsa pozitif ofsetli borsalarda (BIST +03) split gunu
sabahinin barlari yanlis tarafta kalir.

Revision ID: e7f2a94c1b83
Revises: d4e8b1f37a25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e7f2a94c1b83"
down_revision: str | None = "d4e8b1f37a25"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Mevcut satirlar NULL kalir; bir sonraki kosuda history_metadata
    # dataset'i onlari doldurur (upsert, update_columns kapsaminda).
    op.add_column(
        "history_metadata",
        sa.Column("exchange_timezone_name", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("history_metadata", "exchange_timezone_name")
