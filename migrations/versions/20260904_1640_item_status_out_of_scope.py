"""sync_run_items.status -> 'out_of_scope' (PB S6.5c)

ENUM degisikligini Alembic autogenerate YAKALAMAZ; elle yazilmistir
(20260904_1310 ayni sebeple boyle yazilmisti).

Neden NOT_ATTEMPTED yeniden kullanilmadi: RunTally.exit_code
"islenmemis sembol varken cikis kodu asla 0 olamaz" invariant'ini
uyguluyor (runner.py:752). 1m kapsami disindaki ~4.500 sembole
not_attempted yazmak `yfin sync`i HER GUN exit 2 ve
sync_runs.status='partial' yapardi. Kapsam disilik bir eksiklik degil,
kasitli bir karardir.

Revision ID: d4e8b1f37a25
Revises: a1c3e7d92f04
"""

from __future__ import annotations

from alembic import op

revision: str = "d4e8b1f37a25"
down_revision: str | None = "a1c3e7d92f04"
branch_labels: str | None = None
depends_on: str | None = None

_NEW = (
    "ENUM('ok','empty','skipped','failed','unknown_symbol'," "'not_attempted','out_of_scope')"
)
_OLD = "ENUM('ok','empty','skipped','failed','unknown_symbol','not_attempted')"


def upgrade() -> None:
    op.execute(f"ALTER TABLE sync_run_items MODIFY COLUMN status {_NEW} NOT NULL")


def downgrade() -> None:
    # Geri alirken bilgi kaybi olmasin: out_of_scope satirlari, anlamca en
    # yakin olan skipped'a cevrilir (ikisi de "kosturulmadi" ailesidir).
    op.execute("UPDATE sync_run_items SET status = 'skipped' WHERE status = 'out_of_scope'")
    op.execute(f"ALTER TABLE sync_run_items MODIFY COLUMN status {_OLD} NOT NULL")
