"""`run_domain_sync` uctan uca: sira, hucre sayisi, bolge ekseni (SI S9.3, S6.7).

AG YOKTUR: `fetch_domain` fixture'lardan besleyen bir sahte ile degistirilir.
Geri kalan her sey URETIM YOLUDUR -- gercek dataset'ler, gercek runner,
gercek PostgreSQL, gercek `sync_run_items`.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from helpers import FIXTURE_ROOT, domain_data
from yfin.config import Settings
from yfin.datasets.registry import DOMAIN_DATASETS
from yfin.domain_runner import run_domain_sync
from yfin.models import ItemStatus, RunScope

pytestmark = pytest.mark.repo

# Fixture'i olan alt evren. Endustri anahtarlari sektor yanitindan
# KESFEDILIR; sahte cekim yalniz fixture'i olanlara yanit verir.
SECTORS = ("technology",)
INDUSTRIES = ("semiconductors", "electronic-components")

class FakeYahoo:
    """Fixture'i olmayan anahtarda 404 taklidi yapar -> `failed` (SI S8.2)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, key: str, domain_type: str, region: str) -> dict[str, Any]:
        self.calls.append((key, domain_type, region))
        # `domain_data` fixture yoksa `pytest.skip` atar (BaseException);
        # burada istenen sey ATLAMAK degil, 404 taklidi yapmaktir.
        suffix = "" if region == "US" else f".{region}"
        path = FIXTURE_ROOT / "_domain" / domain_type / f"{key}{suffix}.json"
        if not path.exists():
            raise RuntimeError(f"404 Client Error: Not Found for url: {key}")
        return domain_data(domain_type, key, region)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> FakeYahoo:
    fake = FakeYahoo()
    for module in (
        "yfin.datasets.domain.taxonomy",
        "yfin.datasets.domain.profile",
        "yfin.datasets.domain.rankings",
        "yfin.domain_runner",
    ):
        monkeypatch.setattr(f"{module}.fetch_domain", fake, raising=True)
    # Bootstrap yalniz fixture'i olan sektorleri dolassin
    monkeypatch.setattr("yfin.datasets.domain.taxonomy.SECTOR_KEYS", SECTORS)
    return fake


@pytest.fixture
def clean(test_engine: Engine) -> Any:
    yield
    with test_engine.connect() as conn:
        for table in (
            "sync_run_items",
            "sync_runs",
            "domain_asof_state",
            "domain_report_links",
            "research_reports",
            "domain_top_movers",
            "domain_top_funds",
            "domain_top_companies",
            "domain_metrics",
            "domains",
        ):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM symbols WHERE quote_type = 'INDEX'"))
        conn.commit()


def _settings(regions: str = "US") -> Settings:
    return Settings(yf_domain_regions=regions, yf_domain_reference_sector="technology")


def _run(engine: Engine, regions: str = "US") -> Any:
    return run_domain_sync(
        engine,
        DOMAIN_DATASETS.resolve(None),
        settings=_settings(regions),
        acquire_lock=False,
    )


@pytest.mark.usefixtures("clean")
def test_full_run_writes_the_taxonomy_and_records_every_cell(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    tally = _run(test_engine)
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        run = session.execute(
            text("SELECT scope, status, symbol_count, selector FROM sync_runs WHERE id = :i"),
            {"i": tally.run_id},
        ).one()
        assert run.scope == RunScope.DOMAIN.value
        assert run.symbol_count == 0
        assert run.selector == "regions=US"

        sectors = session.execute(
            text("SELECT COUNT(*) FROM domains WHERE domain_type = 'sector'")
        ).scalar_one()
        industries = session.execute(
            text("SELECT COUNT(*) FROM domains WHERE domain_type = 'industry'")
        ).scalar_one()
        assert sectors == 1
        assert industries == 12

        # Bootstrap turu: 2 hucre, `symbol` alani '*'
        bootstrap = session.execute(
            text(
                "SELECT table_name, symbol, region FROM sync_run_items "
                "WHERE run_id = :i AND dataset = 'domain_taxonomy' ORDER BY table_name"
            ),
            {"i": tally.run_id},
        ).all()
        assert [r.table_name for r in bootstrap] == ["domains", "symbols"]
        assert {r.symbol for r in bootstrap} == {"*"}


@pytest.mark.usefixtures("clean")
def test_sync_run_items_carry_the_domain_symbol_not_the_key(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    """`symbol` VARCHAR(32); bes endustri anahtari bunu asiyor (en uzun 37)."""
    tally = _run(test_engine)
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        rows = session.execute(
            text(
                "SELECT DISTINCT i.symbol FROM sync_run_items i "
                "WHERE i.run_id = :r AND i.dataset = 'industry_profile'"
            ),
            {"r": tally.run_id},
        ).scalars()
        symbols = set(rows)
        assert symbols
        assert all(s.startswith("^YH") for s in symbols)


@pytest.mark.usefixtures("clean")
def test_region_column_is_filled_for_regional_datasets_only(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    tally = _run(test_engine)
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        pairs = {
            (row.dataset, row.region)
            for row in session.execute(
                text("SELECT DISTINCT dataset, region FROM sync_run_items WHERE run_id = :r"),
                {"r": tally.run_id},
            )
        }
    assert ("sector_rankings", "US") in pairs
    assert ("sector_profile", "*") in pairs
    assert ("domain_taxonomy", "*") in pairs
    # Sembol ve piyasa tarafi NULL kalir; burada her hucre doludur.
    assert all(region is not None for _dataset, region in pairs)


@pytest.mark.usefixtures("clean")
def test_missing_fixture_becomes_failed_not_empty(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    """404 -> `failed`. Kural gevsetilseydi kayip endustriler SESSIZCE
    `empty` yazilir ve denetim "hata yok" derdi (SI S8.2)."""
    tally = _run(test_engine)
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        failed = session.execute(
            text(
                "SELECT DISTINCT symbol, dataset FROM sync_run_items "
                "WHERE run_id = :r AND status = 'failed'"
            ),
            {"r": tally.run_id},
        ).all()
        errors = session.execute(
            text(
                "SELECT DISTINCT error FROM sync_run_items "
                "WHERE run_id = :r AND status = 'failed' LIMIT 1"
            ),
            {"r": tally.run_id},
        ).scalar()
    # 12 endustrinin yalniz ikisinin fixture'i var; kalani 404 verir.
    assert failed
    assert "404" in (errors or "")
    assert tally.exit_code() != 0


@pytest.mark.usefixtures("clean")
def test_cells_exist_for_every_table_of_every_dataset(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    tally = _run(test_engine)
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        rows = session.execute(
            text(
                "SELECT dataset, COUNT(DISTINCT table_name) AS tables, COUNT(*) AS cells "
                "FROM sync_run_items WHERE run_id = :r GROUP BY dataset"
            ),
            {"r": tally.run_id},
        ).all()
    by_dataset = {r.dataset: (r.tables, r.cells) for r in rows}
    # Her dataset TAM olarak `produces` kadar tablo bildirir
    for name, (tables, _cells) in by_dataset.items():
        assert tables == len(DOMAIN_DATASETS[name].produces), name
    # Beklenen hucre sayisi: 2 + 4*1 + 3*1 + 5*12 + 3*12
    assert sum(cells for _tables, cells in by_dataset.values()) == 2 + 4 + 3 + 60 + 36


@pytest.mark.usefixtures("clean")
def test_two_regions_double_only_the_regional_datasets(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    tally = _run(test_engine, regions="US,GB")
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        rows = session.execute(
            text(
                "SELECT dataset, region, COUNT(*) AS cells FROM sync_run_items "
                "WHERE run_id = :r GROUP BY dataset, region"
            ),
            {"r": tally.run_id},
        ).all()
    per_dataset: dict[str, set[str]] = {}
    for row in rows:
        per_dataset.setdefault(row.dataset, set()).add(row.region)
    assert per_dataset["sector_rankings"] == {"US", "GB"}
    assert per_dataset["industry_rankings"] == {"US", "GB"}
    assert per_dataset["sector_profile"] == {"*"}
    assert per_dataset["industry_profile"] == {"*"}


@pytest.mark.usefixtures("clean")
def test_second_run_marks_data_cells_skipped(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    _run(test_engine)
    second = _run(test_engine)
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        statuses = {
            (row.dataset, row.table_name): row.status
            for row in session.execute(
                text(
                    "SELECT dataset, table_name, status FROM sync_run_items "
                    "WHERE run_id = :r AND dataset = 'sector_rankings'"
                ),
                {"r": second.run_id},
            )
        }
    assert statuses[("sector_rankings", "domain_top_companies")] == ItemStatus.SKIPPED.value
    assert statuses[("sector_rankings", "domain_asof_state")] == ItemStatus.OK.value


@pytest.mark.usefixtures("clean")
def test_industry_keys_come_from_the_database(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    """Anahtarlar bellekte TASINMAZ; runner onlari `domains`tan okur."""
    _run(test_engine)
    requested = {key for key, kind, _ in fake_fetch.calls if kind == "industry"}
    factory = sessionmaker(bind=test_engine, future=True)
    with factory() as session:
        stored = set(
            session.execute(
                text("SELECT domain_key FROM domains WHERE domain_type = 'industry'")
            ).scalars()
        )
    assert requested == stored


@pytest.mark.usefixtures("clean")
def test_sector_datasets_run_before_industry_datasets(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    _run(test_engine)
    kinds = [kind for _key, kind, _region in fake_fetch.calls]
    first_industry = kinds.index("industry")
    assert "sector" not in kinds[first_industry:]


@pytest.mark.usefixtures("clean")
def test_one_key_and_region_is_a_single_http_request(
    test_engine: Engine, fake_fetch: FakeYahoo
) -> None:
    """Bir (anahtar, bolge) cifti TEK istektir ve o ciftin TUM
    dataset'lerini besler (SI S4.5).

    BASARISIZ cekim onbellege GIRMEZ ve bir sonraki dataset onu yeniden
    dener; bu istenen davranistir -- tur bazli hata siniri gecici bir agi
    hatasinda tek bir dataset'i degil, o (anahtar, bolge) turunu dusurur ve
    kardes dataset'in yeniden denemesi ucuz bir kurtarma sansidir.
    """
    _run(test_engine)
    successful = [
        call
        for call in fake_fetch.calls
        if (FIXTURE_ROOT / "_domain" / call[1] / f"{call[0]}.json").exists()
    ]
    assert successful
    assert len(successful) == len(set(successful))

