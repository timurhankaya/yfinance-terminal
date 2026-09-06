"""`domain_research_reports` -> `research_reports` + iki kolon (SQ S12.1/Rev1)

Rapor kimlikleri TEK UZAYDIR: `Sector.research_reports` ile `Search.research`
ayni saglayicidan, ayni bicimde
(`<SAGLAYICI>_<KAYNAK_KIMLIK>_<Tur>_<epoch_ms>`) kimlik dondururler --
`ARGUS_48138_TechnicalAnalysis_1788520901000` ve
`ARGUS_2660_AnalystReport_1785496444000`. Iki ayri tablo ayni raporu iki kez,
FARKLI kolon altkumeleriyle tutardi (SQ K8).

Bu revision KOD DEGISIKLIGIYLE AYNI COMMIT'TE gider: SI dataset katmani
CANLIDIR (5 kayitli dataset + `domain_runner`) ve `REPORTS_TABLE` sabiti,
`prune.py` yetim-rapor temizligi ile modellerin hepsi ayni tablo adina
bakar. Ayri gitseydi domain kosusu `ERROR 1146` verirdi.

RENAME TABLE MySQL 8'de yalnizca metadata'dir; tablo boyutundan bagimsiz
sabit surelidir. Iki kolon eklemesi ALGORITHM=INSTANT.

`domain_report_links` ADINI KORUR: o bag DOMAIN'e ozgudur ve Search
tarafinin kendi bag tablosu (`search_report_hits`, Rev2) ayrica acilir.
Yalnizca FK'sinin HEDEF tablo adi degisir; MySQL bunu RENAME TABLE
sirasinda kendisi gunceller, bu yuzden FK'yi elle dusurup kurmaya gerek
YOKTUR.

Revision ID: d1a4f7c2e8b6
Revises: c5b1d0e6a942
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1a4f7c2e8b6"
down_revision: str | None = "c5b1d0e6a942"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.rename_table("domain_research_reports", "research_reports")

    # Index adi tabloyla birlikte TASINMAZ; MySQL onu eski adiyla tutar.
    # Elle yeniden adlandirilmazsa `Base.metadata` ile fiili sema ayrisir ve
    # `test_discovery_schema` gibi index adi dogrulayan testler kirilir.
    op.execute(
        "ALTER TABLE research_reports RENAME INDEX ix_domain_research_reports_date "
        "TO ix_research_reports_date"
    )

    # Yalniz SEARCH yolunda dolar; domain yaniti bu iki alani TASIMAZ.
    op.add_column("research_reports", sa.Column("author", sa.String(128), nullable=True))
    op.add_column("research_reports", sa.Column("report_headline", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("research_reports", "report_headline")
    op.drop_column("research_reports", "author")
    op.execute(
        "ALTER TABLE research_reports RENAME INDEX ix_research_reports_date "
        "TO ix_domain_research_reports_date"
    )
    op.rename_table("research_reports", "domain_research_reports")
