"""Client repository against a real PostgreSQL schema.

The invariant under test throughout is `auth_epoch`: every change that
narrows what a token may do has to bump it, because token verification
never reads the database and the epoch is the only thing that can make
such a change effective before the token expires.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import Session, sessionmaker

from yfin.api.auth.hashing import verify_against
from yfin.api.models.clients import ApiClient, ApiClientScope, ApiClientSecret, ApiScope
from yfin.api.models.plans import ApiPlan
from yfin.api.storage import clients as repo

pytestmark = pytest.mark.repo

# The column stores a contact string and validates nothing, so the tests
# use a plain label rather than an address.
OWNER = "test-owner"

PLANS = (
    ("free", 2, 10, 50_000, 100, 2),
    ("pro", 50, 200, 20_000_000, 1_000, 8),
)


@pytest.fixture
def session(test_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.execute(
            insert(ApiPlan),
            [
                {
                    "plan": plan,
                    "requests_per_second": rps,
                    "burst": burst,
                    "monthly_quota": quota,
                    "max_page_size": page,
                    "max_concurrency": concurrency,
                }
                for plan, rps, burst, quota, page, concurrency in PLANS
            ],
        )
        session.commit()
        yield session
        session.rollback()
        for model in (ApiClientScope, ApiClientSecret, ApiClient, ApiPlan):
            session.query(model).delete()
        session.commit()


def _create(session: Session, plan: str = "free") -> repo.CreatedClient:
    created = repo.create_client(
        session,
        name="test client",
        owner_email=OWNER,
        plan=plan,
        scopes=[ApiScope.BARS.value, ApiScope.REFERENCE.value],
    )
    session.commit()
    return created


def test_olusturulan_secret_DOGRULANIR(session: Session) -> None:
    created = _create(session)
    candidates = repo.candidates_for(session, created.client_id)
    assert verify_against(created.secret, candidates) is not None


def test_secret_veritabaninda_ACIK_DURMAZ(session: Session) -> None:
    created = _create(session)
    stored = session.scalars(
        select(ApiClientSecret.secret_hash).where(
            ApiClientSecret.client_id == created.client_id
        )
    ).all()
    assert stored and all(created.secret not in row for row in stored)


def test_scope_satirlari_yazilir(session: Session) -> None:
    created = _create(session)
    assert repo.scopes_of(session, created.client_id) == sorted(
        [ApiScope.BARS.value, ApiScope.REFERENCE.value]
    )


def test_bilinmeyen_plan_reddedilir(session: Session) -> None:
    with pytest.raises(repo.UnknownPlan):
        repo.create_client(session, name="x", owner_email=OWNER, plan="yok", scopes=[])


def test_rotasyon_ESKI_SECRETI_calisir_birakir(session: Session) -> None:
    """The whole point of a separate secrets table: a client switches on
    its own schedule instead of taking an outage."""
    created = _create(session)
    new_secret = repo.rotate_secret(session, created.client_id)
    session.commit()

    candidates = repo.candidates_for(session, created.client_id)
    assert verify_against(new_secret, candidates) is not None
    assert verify_against(created.secret, candidates) is not None


def test_rotasyon_eski_secrete_SON_KULLANMA_koyar(session: Session) -> None:
    created = _create(session)
    repo.rotate_secret(session, created.client_id, grace=timedelta(days=3))
    session.commit()

    rows = session.scalars(
        select(ApiClientSecret)
        .where(ApiClientSecret.client_id == created.client_id)
        .order_by(ApiClientSecret.created_at)
    ).all()
    assert rows[0].expires_at is not None
    assert rows[-1].expires_at is None


def test_ucuncu_rotasyon_REDDEDILIR(session: Session) -> None:
    """Two live secrets mean an unfinished rotation; a third would make it
    unclear which one the client is actually using."""
    created = _create(session)
    repo.rotate_secret(session, created.client_id)
    session.commit()
    with pytest.raises(repo.TooManyLiveSecrets):
        repo.rotate_secret(session, created.client_id)


def test_suresi_gecmis_secret_ARTIK_KULLANILAMAZ(session: Session) -> None:
    created = _create(session)
    row = session.scalars(
        select(ApiClientSecret).where(ApiClientSecret.client_id == created.client_id)
    ).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    candidates = repo.candidates_for(session, created.client_id)
    assert verify_against(created.secret, candidates) is None
    # ...but it is still returned as a candidate, so the verification path
    # keeps costing the same whether the secret is expired or simply wrong.
    assert candidates


def test_iptal_edilen_secret_ARTIK_KULLANILAMAZ(session: Session) -> None:
    created = _create(session)
    secret_id = session.scalars(
        select(ApiClientSecret.id).where(ApiClientSecret.client_id == created.client_id)
    ).one()
    repo.revoke_secret(session, created.client_id, secret_id)
    session.commit()

    candidates = repo.candidates_for(session, created.client_id)
    assert verify_against(created.secret, candidates) is None


# --- auth_epoch invaryanti --------------------------------------------------


def _epoch(session: Session, client_id: str) -> int:
    session.expire_all()
    return repo.epoch_of(session, client_id)


def test_secret_iptali_EPOCHU_ARTIRIR(session: Session) -> None:
    created = _create(session)
    before = _epoch(session, created.client_id)
    secret_id = session.scalars(
        select(ApiClientSecret.id).where(ApiClientSecret.client_id == created.client_id)
    ).one()
    repo.revoke_secret(session, created.client_id, secret_id)
    session.commit()
    assert _epoch(session, created.client_id) > before


def test_scope_daraltmasi_EPOCHU_ARTIRIR(session: Session) -> None:
    """Narrowing a scope must bite now, not when the token runs out."""
    created = _create(session)
    before = _epoch(session, created.client_id)
    repo.set_scopes(session, created.client_id, [ApiScope.REFERENCE.value])
    session.commit()
    assert _epoch(session, created.client_id) > before
    assert repo.scopes_of(session, created.client_id) == [ApiScope.REFERENCE.value]


def test_plan_degisikligi_EPOCHU_ARTIRIR(session: Session) -> None:
    created = _create(session)
    before = _epoch(session, created.client_id)
    repo.set_plan(session, created.client_id, "pro")
    session.commit()
    assert _epoch(session, created.client_id) > before


def test_pasiflestirme_ve_etkinlestirme_EPOCHU_ARTIRIR(session: Session) -> None:
    created = _create(session)
    before = _epoch(session, created.client_id)
    repo.set_active(session, created.client_id, False)
    session.commit()
    after_disable = _epoch(session, created.client_id)
    assert after_disable > before

    client = session.get(ApiClient, created.client_id)
    assert client is not None and client.disabled_at is not None

    repo.set_active(session, created.client_id, True)
    session.commit()
    assert _epoch(session, created.client_id) > after_disable
    session.expire_all()
    client = session.get(ApiClient, created.client_id)
    assert client is not None and client.disabled_at is None


def test_rotasyon_EPOCHU_ARTIRIR(session: Session) -> None:
    created = _create(session)
    before = _epoch(session, created.client_id)
    repo.rotate_secret(session, created.client_id)
    session.commit()
    assert _epoch(session, created.client_id) > before


def test_bilinmeyen_istemci_ACIK_HATA(session: Session) -> None:
    with pytest.raises(repo.UnknownClient):
        repo.set_active(session, "yfc_yok", True)
