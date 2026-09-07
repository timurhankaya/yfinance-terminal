# Web Terminal 1a: İskelet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tarayıcıdan `/ui`'ye girip şifreyle oturum açan kullanıcının `/ui/t/AAPL/DES` adresinde sembol özetini görebildiği, API sürecinden servis edilen, CI ve Docker'a bağlanmış SPA iskeleti.

**Architecture:** React + Vite SPA `web/` altında derlenir, çıktısı `src/yfin/ui/static/dist`'e yazılır ve `yfin.ui.install(app, settings)` bunu `/ui` altından elle yazılmış rotalarla servis eder (FastAPI `app.frontend()` kullanılmaz). Oturum, mevcut `jwt_signing_key` ile imzalanan `aud="yfin-ui"` JWT çerezidir; `current_principal` Bearer yoksa çereze düşer ve `meter` `client_id="ui"` için ölçümü atlar. UI kapalıyken (`YFAPI_UI_ENABLED=false`, varsayılan) `yfin.ui` import edilmez ve API bugünkü gibi çalışır.

**Tech Stack:** Python 3.13, FastAPI 0.141, PyJWT, pydantic-settings; React 19, Vite 8, TypeScript, react-router 7, vitest, @testing-library/react; Node 22.

**Spec:** `docs/superpowers/specs/2026-09-07-web-terminal-design.md` (bölümler: Kararlar 1-5, 9; Mimari; Kimlik doğrulama; Dosyalar; Uygulama sırası 1a; Testler)

## Global Constraints

- Committed `openapi.json` **byte-byte değişmez**: her yeni rota `include_in_schema=False`. `uv run python scripts/dump_openapi.py --check` her task sonunda yeşil.
- `uv run ruff check .`, `uv run mypy` (strict), `uv run pytest -q` her commit öncesi yeşil; her task'ın "Lint, tip, kontrat" adımı dördünü birden koşar. Unit testler loopback dışına çıkamaz (`tests/unit/conftest.py`).
- Testlerde `ApiSettings` her zaman `_env_file=None` ile kurulur ve `jwt_signing_key`, `jwt_kid="k1"`, `jwt_issuer="yfin-api"` açıkça verilir; geliştiricinin `.env` dosyası test sonucunu değiştiremez.
- Yeni `ApiSettings` alanı `.env.example`'a `YFAPI_<AD>=` satırı olarak **girmek zorunda** (`tests/unit/test_env_example.py::test_every_api_setting_is_documented`).
- UI rotaları `/ui`, `/ui/t/{path}`, `/ui/assets/*`, `/ui/api/*`. `/ui/api` altında bilinmeyen yol 404 problem gövdesi döner, asla `index.html`.
- Çerez adı `yfin_ui`; `HttpOnly`, `SameSite=Lax`, `Path=/`; `Secure` yalnız `public_base_url` `https://` ile başlıyorsa. Süre 24 saat.
- `client_id == "ui"` sentinel; `UI_PAGE_CAP = 1000`; UI istekleri rate/quota/concurrency'nin üçünü de atlar.
- Login limiti `client_ip` başına dakikada 5 (`LOGIN_ATTEMPTS_PER_MINUTE = 5`), süreç içi `FixedWindow`.
- CSP (yalnız `index.html` yanıtında): `default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'` ve `X-Frame-Options: DENY`. Plan kararı olarak `Cache-Control: no-store` da aynı yanıtta. `style-src 'self'` inline `style=` özniteliğini yasaklar; React kodunda inline stil kullanılmaz.
- `Me.expires_at` UNIX epoch saniye (`int | null`); spec'teki "iso" ifadesi bu plana göre güncellendi.
- 1a'da `@tanstack/react-query` kullanılmaz (`useState` + `fetch`); Query'ye geçiş 1b'nin kararı.
- Yorumlar ve docstring'ler kod tabanının dilinde (İngilizce); UI metinleri İngilizce.
- Commit mesajlarında e-posta adresi içeren trailer **kullanılmaz** (commit hook reddeder). Her commit `Claude-Session:` satırı ile biter.
- Python testleri npm'e bağımlı değildir; `npm ci` yalnız `web` CI job'unda, Docker `web` aşamasında ve geliştirici makinesinde koşar.

Test sabitleri: aşağıdaki testlerde imza anahtarı `KEY = "k" * 32`, UI şifresi `PW = "hunter2"` olarak modül düzeyinde tanımlanır ve her yerde bu adlarla kullanılır. Her `ApiSettings` kurulumu `_env_file=None, jwt_kid="k1", jwt_issuer="yfin-api"` taşır.

---

## Dosya yapısı

| Dosya | Sorumluluk |
| --- | --- |
| `src/yfin/api/core/config.py` | `ui_enabled`, `ui_password` alanları |
| `src/yfin/api/core/window.py` | `FixedWindow` (meta.py'den taşınır; login limiti de kullanır) |
| `src/yfin/api/routers/meta.py` | `_FixedWindow = FixedWindow` re-export, davranış değişmez |
| `src/yfin/ui/__init__.py` | `install(app, settings, dist_dir=None)`: rotaları ve statik servisi kurar |
| `src/yfin/ui/session.py` | `issue(settings) -> (token, expires_at)`, `verify(settings, token) -> UiClaims`, çerez sabitleri |
| `src/yfin/ui/router.py` | `/ui/api/login`, `/ui/api/logout`, `/ui/api/me`, catch-all 404; `UiSession` bağımlılığı |
| `src/yfin/ui/pages.py` | `/ui`, `/ui/t/{path:path}` → `index.html` + CSP; `/ui/assets` StaticFiles |
| `src/yfin/api/auth/dependencies.py` | `UI_CLIENT_ID`, `UI_PAGE_CAP`, çerez dalı |
| `src/yfin/api/ratelimit/dependencies.py` | `meter` UI erken dönüşü |
| `src/yfin/api/app.py` | koşullu `ui.install` |
| `web/` | SPA: `src/api/client.ts`, `src/app/{main,App,Shell,LoginModal,session}.tsx`, `src/app/styles.css`, `src/panels/DES.tsx`, testler |
| `.gitignore`, `.dockerignore` | `web/node_modules/` (T7); Docker bağlamı dışında tutulanlar (T10) |
| `pyproject.toml`, `Dockerfile`, `.github/workflows/ci.yml`, `docker-compose.yml`, `.env.example`, `README.md` | paketleme ve belgeleme |

`.gitignore` zaten `dist/` desenini içeriyor; `src/yfin/ui/static/dist` otomatik yok sayılır. `web/node_modules/` T7'de eklenir.

---

### Task 1: UI ayarları ve başlangıç doğrulaması

**Files:**
- Modify: `src/yfin/api/core/config.py:44-56`
- Modify: `.env.example` (`YFAPI_DOCS_ENABLED` satırının altına)
- Test: `tests/unit/test_ui_settings.py`

**Interfaces:**
- Produces: `ApiSettings.ui_enabled: bool`, `ApiSettings.ui_password: str`, `ApiSettings.ui_cookie_secure() -> bool`, `ApiSettings.validate_ui() -> None` (raises `ValueError`).

- [ ] **Step 1: Failing test'i yaz**

```python
# tests/unit/test_ui_settings.py
"""UI settings: the switch, the password and the cookie's Secure flag."""

from __future__ import annotations

import pytest

from yfin.api.core.config import ApiSettings

PW = "hunter2"


def test_ui_is_off_by_default() -> None:
    # _env_file=None: the developer's own .env must not decide this test.
    assert ApiSettings(_env_file=None).ui_enabled is False
    assert ApiSettings(_env_file=None).ui_password == ""


def test_enabled_without_a_password_is_REFUSED() -> None:
    """A UI with an empty password is not "no auth", it is a lie: the login
    form would accept the empty string. Refuse at startup instead."""
    with pytest.raises(ValueError, match="YFAPI_UI_PASSWORD"):
        ApiSettings(ui_enabled=True, ui_password="").validate_ui()


def test_enabled_with_a_password_validates() -> None:
    ApiSettings(ui_enabled=True, ui_password=PW).validate_ui()


def test_disabled_never_validates_the_password() -> None:
    ApiSettings(ui_enabled=False, ui_password="").validate_ui()


def test_cookie_is_secure_only_behind_https() -> None:
    assert ApiSettings(public_base_url="https://yfin.example").ui_cookie_secure() is True
    assert ApiSettings(public_base_url="http://localhost:8000").ui_cookie_secure() is False
    assert ApiSettings(public_base_url="").ui_cookie_secure() is False
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

Run: `uv run pytest tests/unit/test_ui_settings.py -v`
Expected: FAIL, `AttributeError: 'ApiSettings' object has no attribute 'validate_ui'` (ilk test de `ui_enabled` için `AttributeError` verir).

- [ ] **Step 3: Alanları ve metotları ekle**

`src/yfin/api/core/config.py` içinde `public_base_url` alanının altına:

```python
    # --- web terminal -----------------------------------------------------
    # Off by default: a deployment that has not opted in serves nothing
    # under /ui and never imports yfin.ui. The password is a plain string
    # in the environment on purpose -- one operator, one secret, nothing
    # a hash would protect (see the web terminal design, "Kimlik
    # doğrulama").
    ui_enabled: bool = False
    ui_password: str = ""
```

`trusted_proxy_list` metodunun altına:

```python
    def validate_ui(self) -> None:
        """Refuses a UI that is switched on with nothing guarding it.

        Called from `create_app`, not at field level, for the same reason
        as `signing_key_bytes`: a CLI command that never serves the UI
        must not fail because the UI is misconfigured.
        """
        if self.ui_enabled and not self.ui_password:
            raise ValueError("YFAPI_UI_ENABLED is on but YFAPI_UI_PASSWORD is empty")

    def ui_cookie_secure(self) -> bool:
        """`Secure` only when the deployment says it is behind TLS. An empty
        base URL means plain HTTP on localhost, where a Secure cookie is
        silently dropped by the browser and login appears to do nothing."""
        return self.public_base_url.lower().startswith("https://")
```

`.env.example`'da `YFAPI_DOCS_ENABLED` satırının altına (yorum dahil):

```dotenv
# Web terminal (the browser UI under /ui). Off by default. When on, the
# password is required: the API refuses to start with it empty.
YFAPI_UI_ENABLED=false
YFAPI_UI_PASSWORD=
```

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `uv run pytest tests/unit/test_ui_settings.py tests/unit/test_env_example.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, tip, kontrat**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`
Expected: temiz.

- [ ] **Step 6: Commit**

```bash
git add src/yfin/api/core/config.py .env.example tests/unit/test_ui_settings.py
git commit -m "feat(ui): YFAPI_UI_ENABLED and YFAPI_UI_PASSWORD settings

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 2: `FixedWindow`'u `api/core/window.py`'ye taşı

**Files:**
- Create: `src/yfin/api/core/window.py`
- Modify: `src/yfin/api/routers/meta.py` (`class _FixedWindow` gövdesi, 46-67; `_limiter = _FixedWindow()` satırı, 88)
- Test: `tests/unit/test_window.py`

**Interfaces:**
- Produces: `yfin.api.core.window.FixedWindow` with `allow(key: str, limit: int) -> bool`.
- `meta._FixedWindow` ve `meta._limiter` adları korunur (`tests/unit/test_api_app.py:160` monkeypatch'ler).

- [ ] **Step 1: Failing test'i yaz**

```python
# tests/unit/test_window.py
"""The in-process per-key minute window, shared by /health/ready and the
UI login endpoint."""

from __future__ import annotations

import pytest

from yfin.api.core.window import FixedWindow


def test_allows_up_to_the_limit_then_refuses() -> None:
    window = FixedWindow()
    assert window.allow("k", 2)
    assert window.allow("k", 2)
    assert not window.allow("k", 2)


def test_keys_are_independent() -> None:
    window = FixedWindow()
    assert window.allow("a", 1)
    assert window.allow("b", 1)
    assert not window.allow("a", 1)


def test_the_window_resets_when_the_minute_rolls(monkeypatch: pytest.MonkeyPatch) -> None:
    import yfin.api.core.window as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    window = FixedWindow()
    assert window.allow("k", 1)
    assert not window.allow("k", 1)
    now[0] += 60
    assert window.allow("k", 1)


def test_meta_still_exposes_the_old_names() -> None:
    from yfin.api.routers import meta

    assert meta._FixedWindow is FixedWindow
    assert isinstance(meta._limiter, FixedWindow)
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

Run: `uv run pytest tests/unit/test_window.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'yfin.api.core.window'`.

- [ ] **Step 3: Modülü oluştur, meta'yı bağla**

```python
# src/yfin/api/core/window.py
"""A per-key counter in a one-minute window, in process.

Deliberately tiny and deliberately not Redis: it protects single
endpoints from floods (readiness probes, the UI login form), it is not
the API's rate limiter. Under `uvicorn --workers N` each process keeps
its own counts, so the effective limit is N times the configured one.
Both call sites accept that.
"""

from __future__ import annotations

import threading
import time


class FixedWindow:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window = 0
        self._hits: dict[str, int] = {}

    def allow(self, key: str, limit: int) -> bool:
        window = int(time.time() // 60)
        with self._lock:
            if window != self._window:
                self._window = window
                self._hits = {}
            count = self._hits.get(key, 0) + 1
            self._hits[key] = count
            return count <= limit
```

Önce bak: `src/yfin/api/core/window.py` zaten varsa (observability spec'i aynı dosyayı yaratır) modülü yeniden yazma, yalnız testi ve re-export'u ekle.

`src/yfin/api/routers/meta.py`: `class _FixedWindow:` satırından başlayıp `class _ReadinessCache:` satırından önce biten bloğu (docstring dahil) sil; import bloğuna `from yfin.api.core.window import FixedWindow` ekle; `_limiter = _FixedWindow()` satırının hemen üstüne:

```python
#: Kept under the old name: tests and the readiness endpoint reach it
#: through this module, and moving the class must not move them.
_FixedWindow = FixedWindow
```

`threading` import'u `_ReadinessCache` tarafından hâlâ kullanılıyor; silme.

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `uv run pytest tests/unit/test_window.py tests/unit/test_api_app.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, tip, kontrat**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`

- [ ] **Step 6: Commit**

```bash
git add src/yfin/api/core/window.py src/yfin/api/routers/meta.py tests/unit/test_window.py
git commit -m "refactor(api): move the per-key minute window to api/core/window.py

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 3: Oturum çerezi JWT'si (`yfin/ui/session.py`)

**Files:**
- Create: `src/yfin/ui/__init__.py` (şimdilik sadece docstring)
- Create: `src/yfin/ui/session.py`
- Test: `tests/unit/test_ui_session.py`

**Interfaces:**
- Produces:
  - `COOKIE_NAME = "yfin_ui"`, `UI_AUDIENCE = "yfin-ui"`, `SESSION_TTL_SECONDS = 86_400`
  - `UiClaims(jti: str, expires_at: int)` frozen dataclass
  - `issue(settings: ApiSettings) -> tuple[str, int]` → `(token, expires_at_epoch)`
  - `verify(settings: ApiSettings, token: str) -> UiClaims`, raises `SessionInvalid`
- Consumes: `ApiSettings.signing_key_bytes()`, `jwt_issuer`, `jwt_kid`; `yfin.api.auth.jwt.mint/verify` (çapraz ret testleri için).

- [ ] **Step 1: Failing test'i yaz**

```python
# tests/unit/test_ui_session.py
"""The UI session cookie: a JWT with its own audience, so it is not an
access token and an access token is not a session."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from yfin.api.auth import jwt as access
from yfin.api.core.config import ApiSettings
from yfin.ui import session

KEY = "k" * 32
PW = "hunter2"


def settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=True, ui_password=PW,
    )


def test_issue_then_verify_round_trips() -> None:
    token, expires_at = session.issue(settings())
    claims = session.verify(settings(), token)
    assert claims.expires_at == expires_at
    assert len(claims.jti) == 32
    assert expires_at - int(time.time()) == pytest.approx(session.SESSION_TTL_SECONDS, abs=5)


def test_two_sessions_have_different_jti() -> None:
    a, _ = session.issue(settings())
    b, _ = session.issue(settings())
    assert session.verify(settings(), a).jti != session.verify(settings(), b).jti


def test_a_session_cookie_is_NOT_an_access_token() -> None:
    token, _ = session.issue(settings())
    with pytest.raises(access.TokenInvalid):
        access.verify(settings(), token)


def test_an_access_token_is_NOT_a_session_cookie() -> None:
    token, _ = access.mint(
        settings(), client_id="c1", scopes=("bars:read",), secret_id=1, epoch=0
    )
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)


def test_a_token_signed_with_another_key_is_rejected() -> None:
    other = ApiSettings(_env_file=None, jwt_signing_key="x" * 32, jwt_kid="k1", jwt_issuer="yfin-api")
    token, _ = session.issue(other)
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)


def _raw(claims: dict[str, object]) -> str:
    return pyjwt.encode(claims, KEY.encode(), algorithm="HS256", headers={"kid": settings().jwt_kid})


def test_an_expired_session_is_rejected() -> None:
    now = int(time.time())
    token = _raw(
        {"iss": settings().jwt_issuer, "aud": session.UI_AUDIENCE, "iat": now - 100_000,
         "exp": now - 90_000, "jti": "a" * 32}
    )
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)


def test_a_session_without_jti_is_rejected() -> None:
    now = int(time.time())
    token = _raw({"iss": settings().jwt_issuer, "aud": session.UI_AUDIENCE, "iat": now, "exp": now + 100})
    with pytest.raises(session.SessionInvalid):
        session.verify(settings(), token)
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

Run: `uv run pytest tests/unit/test_ui_session.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'yfin.ui'`.

- [ ] **Step 3: Modülleri yaz**

```python
# src/yfin/ui/__init__.py
"""The web terminal: a browser UI served by the API process under /ui.

Nothing here is imported unless `YFAPI_UI_ENABLED` is on -- `create_app`
guards the import -- so a deployment that has not opted in carries no
UI code path at all.
"""
```

```python
# src/yfin/ui/session.py
"""The UI session cookie.

It is a JWT signed with the API's key, and that is safe only because the
audience differs: `verify` in `api/auth/jwt.py` demands `aud=jwt_audience`
and `sid`/`epc`, so a session cookie presented as a Bearer token is
rejected, and the decode here demands `aud=yfin-ui`, so an access token
presented as a cookie is rejected too. Neither side can be replayed as
the other.

Stateless on purpose: `uvicorn --workers 4` gives four processes and no
shared memory, and a session table for one operator is a table nobody
would ever read.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError

from yfin.api.auth.jwt import ALGORITHM, LEEWAY_SECONDS
from yfin.api.core.config import ApiSettings

COOKIE_NAME = "yfin_ui"
UI_AUDIENCE = "yfin-ui"
#: 24 hours, no sliding renewal: a fixed horizon is easier to reason
#: about than "as long as you keep using it", and one login a day is not
#: a burden on a single operator.
SESSION_TTL_SECONDS = 86_400

REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "jti")


class SessionInvalid(Exception):
    """Verification failed. The reason stays internal."""


@dataclass(frozen=True)
class UiClaims:
    jti: str
    expires_at: int


def issue(settings: ApiSettings) -> tuple[str, int]:
    """The encoded cookie value and its expiry as a UNIX timestamp."""
    now = int(time.time())
    expires_at = now + SESSION_TTL_SECONDS
    payload = {
        "iss": settings.jwt_issuer,
        "aud": UI_AUDIENCE,
        "iat": now,
        "exp": expires_at,
        # 128 random bits, hex: the spec's "jti login'de 128 bit rastgele".
        "jti": secrets.token_hex(16),
    }
    encoded = jwt.encode(
        payload,
        settings.signing_key_bytes(),
        algorithm=ALGORITHM,
        headers={"kid": settings.jwt_kid},
    )
    return encoded, expires_at


def verify(settings: ApiSettings, token: str) -> UiClaims:
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise SessionInvalid("unreadable header") from exc
    if str(header.get("kid", "")) != settings.jwt_kid:
        raise SessionInvalid("unknown kid")

    try:
        payload = jwt.decode(
            token,
            settings.signing_key_bytes(),
            algorithms=[ALGORITHM],
            audience=UI_AUDIENCE,
            issuer=settings.jwt_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except InvalidTokenError as exc:
        raise SessionInvalid("rejected") from exc

    return UiClaims(jti=str(payload["jti"]), expires_at=int(payload["exp"]))
```

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `uv run pytest tests/unit/test_ui_session.py -v`
Expected: PASS (7 test).

- [ ] **Step 5: Lint, tip, kontrat**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`

- [ ] **Step 6: Commit**

```bash
git add src/yfin/ui/__init__.py src/yfin/ui/session.py tests/unit/test_ui_session.py
git commit -m "feat(ui): session cookie JWT with its own audience

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 4: `/ui/api` router: login, logout, me, catch-all 404

**Files:**
- Create: `src/yfin/ui/router.py`
- Test: `tests/unit/test_ui_router.py`

**Interfaces:**
- Produces:
  - `router: APIRouter` (prefix `/ui/api`, hepsi `include_in_schema=False`)
  - `UiSession = Annotated[UiClaims, Depends(current_session)]` — çerez yoksa 401 `unauthenticated`, geçersizse 401 `invalid_token`
  - `LOGIN_ATTEMPTS_PER_MINUTE = 5`, `_login_limiter: FixedWindow` (testler monkeypatch'ler)
  - `set_session_cookie(response, settings, token)` ve `clear_session_cookie(response, settings)`
  - `LOGIN_FIELD = "password"`: login formunun alan adı (`Form(alias=LOGIN_FIELD)`); Python parametresi `submitted`.
- Consumes: Task 3 `session.issue/verify/COOKIE_NAME`, Task 2 `FixedWindow`, Task 1 `ui_cookie_secure()`, `yfin.api.core.errors.ApiProblem` ve tipler.

Bu task'ın testi `install()` olmadan çalışır: mini bir FastAPI'ye router eklenir. `install()` Task 6'da gelir.

- [ ] **Step 1: Failing test'i yaz**

```python
# tests/unit/test_ui_router.py
"""Login, logout, me and the /ui/api catch-all."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import install_error_handlers
from yfin.api.core.middleware import RequestContextMiddleware
from yfin.api.core.window import FixedWindow
from yfin.ui import router as ui_router
from yfin.ui.session import COOKIE_NAME

KEY = "k" * 32
PW = "hunter2"
WRONG_PW = "hunter3"
FIELD = ui_router.LOGIN_FIELD


def make_client(monkeypatch: pytest.MonkeyPatch, *, public_base_url: str = "") -> TestClient:
    settings = ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=True, ui_password=PW, public_base_url=public_base_url,
    )
    app = FastAPI()
    app.state.api_settings = settings
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, settings=settings)
    app.include_router(ui_router.router)

    # A throwaway route so the UiSession dependency is exercised in 1a,
    # before any real UI-only data route (1c, 1d) uses it.
    @app.get("/probe", include_in_schema=False)
    def probe(claims: ui_router.UiSession) -> dict[str, str]:
        return {"jti": claims.jti}

    monkeypatch.setattr(ui_router, "_login_limiter", FixedWindow())
    return TestClient(app)


def do_login(client: TestClient, value: str = PW):  # type: ignore[no-untyped-def]
    return client.post("/ui/api/login", data={FIELD: value})


def test_me_without_a_cookie_is_200_and_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/ui/api/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "expires_at": None, "live_enabled": False}


def test_login_sets_an_httponly_lax_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    response = do_login(make_client(monkeypatch))
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie


def test_the_cookie_is_secure_behind_https(monkeypatch: pytest.MonkeyPatch) -> None:
    response = do_login(make_client(monkeypatch, public_base_url="https://yfin.example"))
    assert "Secure" in response.headers["set-cookie"]


def test_me_after_login_is_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    do_login(client)
    body = client.get("/ui/api/me").json()
    assert body["authenticated"] is True
    assert body["expires_at"] is not None


def test_a_wrong_password_is_401_without_a_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    response = do_login(make_client(monkeypatch), WRONG_PW)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "unauthenticated"
    assert "set-cookie" not in response.headers


def test_login_is_limited_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    for _ in range(ui_router.LOGIN_ATTEMPTS_PER_MINUTE):
        assert do_login(client, WRONG_PW).status_code == 401
    limited = do_login(client)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert limited.json()["type"] == "rate_limit_exceeded"


def test_logout_clears_the_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    do_login(client)
    response = client.post("/ui/api/logout")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/ui/api/me").json()["authenticated"] is False


def test_a_tampered_cookie_is_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    client.cookies.set(COOKIE_NAME, "not.a.jwt")
    assert client.get("/ui/api/me").json()["authenticated"] is False


def test_unknown_ui_api_paths_are_404_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/ui/api/no-such-thing", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "not_found"


def test_nothing_under_ui_api_is_in_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/ui") for p in paths)


def test_head_on_an_unknown_ui_api_path_is_404_too(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_client(monkeypatch).head("/ui/api/nope").status_code == 404


def test_ui_session_dependency_without_a_cookie_is_401_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = make_client(monkeypatch).get("/probe")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthenticated"


def test_ui_session_dependency_with_a_bad_cookie_is_401_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(monkeypatch)
    client.cookies.set(COOKIE_NAME, "not.a.jwt")
    response = client.get("/probe")
    assert response.status_code == 401
    assert response.json()["type"] == "invalid_token"


def test_ui_session_dependency_IGNORES_a_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    response = client.get("/probe", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 401


def test_ui_session_dependency_accepts_the_login_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    do_login(client)
    response = client.get("/probe")
    assert response.status_code == 200
    assert len(response.json()["jti"]) == 32
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

Run: `uv run pytest tests/unit/test_ui_router.py -v`
Expected: FAIL, `ImportError: cannot import name 'router' from 'yfin.ui'`.

- [ ] **Step 3: Router'ı yaz**

```python
# src/yfin/ui/router.py
"""The UI's own endpoints: login, logout, who-am-I, and a 404 for
everything else under /ui/api so the SPA fallback can never answer an
API path with HTML.

None of this is in the OpenAPI document. The public contract is `/v1`
and `/oauth`; these routes exist for one browser page and are versioned
with it.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from pydantic import BaseModel

from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import (
    TYPE_INVALID_TOKEN,
    TYPE_NOT_FOUND,
    TYPE_RATE_LIMIT,
    TYPE_UNAUTHENTICATED,
    ApiProblem,
)
from yfin.api.core.window import FixedWindow
from yfin.ui import session
from yfin.ui.session import COOKIE_NAME, SESSION_TTL_SECONDS, SessionInvalid, UiClaims

router = APIRouter(prefix="/ui/api", include_in_schema=False)

#: Per client IP, per process. Behind a reverse proxy this needs
#: YFAPI_TRUSTED_PROXIES, or every login in the world shares one bucket.
LOGIN_ATTEMPTS_PER_MINUTE = 5
_login_limiter = FixedWindow()

#: The form field the login page posts.
LOGIN_FIELD = "password"


class Me(BaseModel):
    authenticated: bool
    expires_at: int | None
    #: Filled in by 1c (the live tick path). Always false until then.
    live_enabled: bool


def _settings(request: Request) -> ApiSettings:
    settings: ApiSettings = request.app.state.api_settings
    return settings


def _claims_from_cookie(request: Request) -> UiClaims | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return session.verify(_settings(request), raw)
    except SessionInvalid:
        return None


def current_session(request: Request) -> UiClaims:
    """The dependency /ui/api routes and the UI-only data routes use.

    Cookie only. A Bearer token is not a session, and accepting one here
    would let an API client reach UI-only routes that bypass metering.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise ApiProblem(401, TYPE_UNAUTHENTICATED, "Login required")
    try:
        return session.verify(_settings(request), raw)
    except SessionInvalid as exc:
        raise ApiProblem(401, TYPE_INVALID_TOKEN, "The session is not valid") from exc


UiSession = Annotated[UiClaims, Depends(current_session)]


def set_session_cookie(response: Response, settings: ApiSettings, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=settings.ui_cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: ApiSettings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=settings.ui_cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def _matches(submitted: str, configured: str) -> bool:
    """Constant time, so a wrong first character costs the same as a
    wrong last one."""
    return hmac.compare_digest(submitted.encode("utf-8"), configured.encode("utf-8"))


@router.post("/login", status_code=204)
def login(
    request: Request,
    response: Response,
    submitted: Annotated[str, Form(alias=LOGIN_FIELD)],
) -> Response:
    settings = _settings(request)
    client_ip = getattr(request.state, "client_ip", "unknown")
    if not _login_limiter.allow(client_ip, LOGIN_ATTEMPTS_PER_MINUTE):
        raise ApiProblem(
            429, TYPE_RATE_LIMIT, "Too many login attempts", headers={"Retry-After": "60"}
        )
    if not _matches(submitted, settings.ui_password):
        raise ApiProblem(401, TYPE_UNAUTHENTICATED, "Wrong password")

    token, _ = session.issue(settings)
    response.status_code = 204
    set_session_cookie(response, settings, token)
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> Response:
    response.status_code = 204
    clear_session_cookie(response, _settings(request))
    return response


@router.get("/me", response_model=Me)
def me(request: Request) -> Me:
    """Always 200: the SPA asks this first and a 401 here would be
    indistinguishable from an expired session mid-use."""
    claims = _claims_from_cookie(request)
    if claims is None:
        return Me(authenticated=False, expires_at=None, live_enabled=False)
    return Me(authenticated=True, expires_at=claims.expires_at, live_enabled=False)


@router.api_route(
    "/{path:path}", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "DELETE", "PATCH"]
)
def not_found(path: str) -> None:
    """Registered last in this router, and this router before the SPA
    pages, so an unknown /ui/api path is a problem document, never
    index.html."""
    raise ApiProblem(404, TYPE_NOT_FOUND, "No such route")
```

`Form()` `python-multipart` gerektirir; `api` extra'sında zaten var. `Form(alias=...)` FastAPI'de form alan adını belirler; parametre adı `submitted` kalır.

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `uv run pytest tests/unit/test_ui_router.py -v`
Expected: PASS (15 test). `SameSite=lax` küçük harf: Starlette böyle yazar.

- [ ] **Step 5: Lint, tip, kontrat**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`

- [ ] **Step 6: Commit**

```bash
git add src/yfin/ui/router.py tests/unit/test_ui_router.py
git commit -m "feat(ui): /ui/api login, logout, me and the catch-all 404

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 5: Çerezli isteklerin `/v1`'e girişi ve ölçüm dışı kalması

**Files:**
- Modify: `src/yfin/api/auth/dependencies.py` (`Principal` 76-82, `current_principal` 151-183)
- Modify: `src/yfin/api/ratelimit/dependencies.py:72-92`
- Test: `tests/unit/test_ui_principal.py`

**Interfaces:**
- Produces: `UI_CLIENT_ID = "ui"`, `UI_PAGE_CAP = 1000`, `UI_SCOPES: frozenset[str]` in `yfin.api.auth.dependencies`.
- Consumes: Task 3 `session.verify`, `COOKIE_NAME`; `scope_for`, `DataFamily`.

- [ ] **Step 1: Failing test'i yaz**

```python
# tests/unit/test_ui_principal.py
"""A session cookie reaches /v1 as the `ui` principal, with every read
scope and no metering at all."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from yfin.api.auth.dependencies import UI_CLIENT_ID, UI_PAGE_CAP, Authenticated, Principal
from yfin.api.core.config import ApiSettings
from yfin.api.core.errors import install_error_handlers
from yfin.api.ratelimit import policy, usage
from yfin.api.ratelimit.dependencies import UsageMiddleware, guard
from yfin.core.families import DataFamily, scope_for
from yfin.ui import session
from yfin.ui.session import COOKIE_NAME

KEY = "k" * 32
PW = "hunter2"


def settings(*, ui_enabled: bool = True) -> ApiSettings:
    return ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=ui_enabled, ui_password=PW,
    )


def make_client(monkeypatch: pytest.MonkeyPatch, *, ui_enabled: bool = True) -> TestClient:
    # `ratelimit/dependencies.py` calls `policy.limits_for_client(...)` and
    # `usage.record(...)` through the module objects, so patching the
    # module attributes is what the code under test sees.
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the UI must never be metered or recorded")

    monkeypatch.setattr(policy, "limits_for_client", explode)
    monkeypatch.setattr(usage, "record", explode)

    app = FastAPI()
    app.state.api_settings = settings(ui_enabled=ui_enabled)
    install_error_handlers(app)
    app.add_middleware(UsageMiddleware)

    @app.get("/who")
    def who(principal: Authenticated) -> dict[str, object]:
        return {"client_id": principal.client_id, "scopes": sorted(principal.scopes)}

    # Same shape as market.py: guard() goes through Depends(), never as a
    # bare default value (FastAPI would treat that as a body parameter).
    @app.get("/bars")
    def bars(
        request: Request,
        principal: Annotated[Principal, Depends(guard(DataFamily.BARS))],
    ) -> dict[str, object]:
        return {"cap": request.state.page_size_cap, "metered": hasattr(request.state, "limits")}

    return TestClient(app)


def with_cookie(client: TestClient) -> TestClient:
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    return client


def test_a_cookie_yields_the_ui_principal_with_every_read_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = with_cookie(make_client(monkeypatch)).get("/who").json()
    assert body["client_id"] == UI_CLIENT_ID
    assert body["scopes"] == sorted(scope_for(f) for f in DataFamily)


def test_a_cookie_passes_a_guarded_route_UNMETERED(monkeypatch: pytest.MonkeyPatch) -> None:
    response = with_cookie(make_client(monkeypatch)).get("/bars")
    assert response.status_code == 200
    assert response.json() == {"cap": UI_PAGE_CAP, "metered": False}


def test_no_credentials_is_still_401(monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(monkeypatch).get("/who")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthenticated"


def test_a_bad_cookie_is_401_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    client.cookies.set(COOKIE_NAME, "not.a.jwt")
    response = client.get("/who")
    assert response.status_code == 401
    assert response.json()["type"] == "invalid_token"


def test_the_cookie_is_IGNORED_when_the_ui_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, ui_enabled=False)
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/who").status_code == 401


def test_a_bearer_header_is_evaluated_ALONE_even_with_a_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header present and invalid must not fall through to the cookie:
    a client that sends both would otherwise get the UI's scopes."""
    client = with_cookie(make_client(monkeypatch))
    response = client.get("/who", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401
    assert response.json()["type"] == "invalid_token"
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

Run: `uv run pytest tests/unit/test_ui_principal.py -v`
Expected: FAIL, `ImportError: cannot import name 'UI_CLIENT_ID'`.

- [ ] **Step 3: Çerez dalını ve ölçüm atlamasını ekle**

`src/yfin/api/auth/dependencies.py`, `Principal` sınıfının üstüne:

```python
#: The web terminal's principal. Not a row in `api_clients`, never
#: metered (see `ratelimit/dependencies.meter`), and it holds every read
#: scope: the single operator behind the UI owns the data.
UI_CLIENT_ID = "ui"
UI_PAGE_CAP = 1000
UI_SCOPES = frozenset(scope_for(f) for f in DataFamily)
```

Import bloğuna `from yfin.core.families import DataFamily, scope_for` ekle.

`current_principal` içindeki

```python
    if not header:
        raise _unauthenticated()
```

satırlarını şununla değiştir:

```python
    if not header:
        # A header, even a bad one, is evaluated alone; the cookie is only
        # consulted when there is no header at all. Otherwise a client
        # holding an expired token and a stray cookie would be promoted.
        principal = _from_session_cookie(request, settings, security_scopes)
        if principal is None:
            raise _unauthenticated()
        return principal
```

ve `current_principal`'ın üstüne yeni fonksiyon:

```python
def _from_session_cookie(
    request: Request, settings: ApiSettings, security_scopes: SecurityScopes
) -> Principal | None:
    """The UI's cookie, when the UI is on. Imported lazily: a deployment
    with the UI off never loads `yfin.ui`."""
    if not settings.ui_enabled:
        return None
    from yfin.ui import session as ui_session

    raw = request.cookies.get(ui_session.COOKIE_NAME)
    if not raw:
        return None
    try:
        claims = ui_session.verify(settings, raw)
    except ui_session.SessionInvalid as exc:
        raise _invalid_token() from exc

    for required in security_scopes.scopes:
        if required not in UI_SCOPES:  # pragma: no cover - every read scope is held
            raise insufficient_scope(required)

    request.state.client_id = UI_CLIENT_ID
    request.state.jti = claims.jti
    return Principal(client_id=UI_CLIENT_ID, scopes=UI_SCOPES, jti=claims.jti)
```

`src/yfin/api/ratelimit/dependencies.py`, `meter` gövdesinin başına (`settings = ...` satırından önce):

```python
    if principal.client_id == UI_CLIENT_ID:
        # The operator's own browser: no plan row, no counters, no slot.
        # `request.state.limits` is deliberately NOT set, which is what
        # keeps UsageMiddleware and attribute_family out of the way.
        request.state.page_size_cap = UI_PAGE_CAP
        return
```

Mevcut `from yfin.api.auth.dependencies import Principal, current_principal` satırına `UI_CLIENT_ID, UI_PAGE_CAP` ekle.

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `uv run pytest tests/unit/test_ui_principal.py tests/unit/test_api_token.py tests/unit/test_api_ratelimit.py tests/unit/test_api_reads.py -v`
Expected: PASS; mevcut Bearer testleri değişmez.

- [ ] **Step 5: Lint, tip, kontrat**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`

- [ ] **Step 6: Commit**

```bash
git add src/yfin/api/auth/dependencies.py src/yfin/api/ratelimit/dependencies.py tests/unit/test_ui_principal.py
git commit -m "feat(api): accept the UI session cookie on /v1, unmetered

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 6: SPA sayfaları, statik servis ve `create_app` montajı

**Files:**
- Create: `src/yfin/ui/pages.py`
- Modify: `src/yfin/ui/__init__.py` (`install` eklenir)
- Modify: `src/yfin/api/app.py:130-150`
- Modify: `pyproject.toml:93-94` (package-data)
- Test: `tests/unit/test_ui_pages.py`

**Interfaces:**
- Produces: `yfin.ui.install(app: FastAPI, settings: ApiSettings, dist_dir: Path | None = None) -> None`; `yfin.ui.pages.CSP: str`; `yfin.ui.pages.default_dist_dir() -> Path`; `yfin.ui.pages.install_pages(app, dist_dir)`.
- Consumes: Task 4 `router`.

- [ ] **Step 1: Failing test'i yaz**

```python
# tests/unit/test_ui_pages.py
"""Serving the SPA: index.html under /ui and /ui/t/*, assets under
/ui/assets, nothing when the build output is absent, and never for an
API path."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.ui import pages

KEY = "k" * 32
PW = "hunter2"


def settings(*, ui_enabled: bool = True, ui_password: str = PW) -> ApiSettings:
    return ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=ui_enabled, ui_password=ui_password, docs_enabled=True,
    )


def build_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>yfin</title><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    return dist


def make_client(dist: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from yfin.api.app import create_app

    # The only way in: create_app with the UI on, pointed at a temp build.
    monkeypatch.setattr(pages, "default_dist_dir", lambda: dist)
    return TestClient(create_app(settings()))


def test_index_is_served_at_ui_and_under_ui_t(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(build_dist(tmp_path), monkeypatch)
    for path in ("/ui", "/ui/", "/ui/t/AAPL/DES", "/ui/t/-/HELP"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        assert "id=root" in response.text


def test_index_carries_the_csp_and_frame_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get("/ui")
    assert response.headers["Content-Security-Policy"] == pages.CSP
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_assets_are_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get("/ui/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_a_missing_asset_is_404_not_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_client(build_dist(tmp_path), monkeypatch).get("/ui/assets/nope.js").status_code == 404


def test_ui_api_is_never_answered_with_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get(
        "/ui/api/whatever", headers={"Accept": "text/html"}
    )
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_v1_404s_are_untouched_by_the_spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = make_client(build_dist(tmp_path), monkeypatch).get("/v1/typo", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_without_a_build_only_the_api_routes_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path / "absent", monkeypatch)
    assert client.get("/ui").status_code == 404
    assert client.get("/ui/api/me").status_code == 200


def test_index_without_an_assets_dir_is_treated_as_no_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    client = make_client(dist, monkeypatch)  # must not raise at startup
    assert client.get("/ui").status_code == 404


def test_with_the_ui_off_yfin_ui_is_never_imported() -> None:
    """This process has already imported yfin.ui (this file does), so the
    claim can only be checked in a fresh interpreter."""
    import subprocess
    import sys

    code = (
        "import sys; from yfin.api.core.config import ApiSettings; "
        "from yfin.api.app import create_app; "
        "create_app(ApiSettings(_env_file=None, jwt_signing_key='k' * 32)); "
        "assert 'yfin.ui' not in sys.modules, 'yfin.ui was imported with the UI off'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_create_app_installs_the_ui_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(build_dist(tmp_path), monkeypatch)
    assert client.get("/ui").status_code == 200
    assert client.get("/ui/api/me").status_code == 200


def test_create_app_leaves_ui_out_when_disabled() -> None:
    from yfin.api.app import create_app

    client = TestClient(create_app(settings(ui_enabled=False)))
    assert client.get("/ui/api/me").status_code == 404
    assert client.get("/ui").status_code == 404


def test_create_app_refuses_enabled_without_a_password() -> None:
    from yfin.api.app import create_app

    with pytest.raises(ValueError, match="YFAPI_UI_PASSWORD"):
        create_app(settings(ui_password=""))


def test_the_openapi_document_has_no_ui_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yfin.api.app import create_app

    monkeypatch.setattr(pages, "default_dist_dir", lambda: build_dist(tmp_path))
    paths = create_app(settings()).openapi()["paths"]
    assert not any(p.startswith("/ui") for p in paths)
```

- [ ] **Step 2: Testin başarısız olduğunu doğrula**

Run: `uv run pytest tests/unit/test_ui_pages.py -v`
Expected: FAIL, `ImportError: cannot import name 'pages' from 'yfin.ui'`.

- [ ] **Step 3: `pages.py`, `install` ve `create_app` bağlantısını yaz**

```python
# src/yfin/ui/pages.py
"""index.html and the built assets, by hand.

FastAPI 0.141 has `app.frontend()`, and it is not used here for three
reasons that were checked, not guessed. Its fallback answers EVERY
unmatched request that accepts text/html with 200 index.html -- a typo
under /v1 would come back as a web page instead of the 404 problem the
contract promises. Its `check_dir` raises at import when the build is
absent, which is every unit test and every unbuilt checkout. And it
offers no hook to put a Content-Security-Policy on the one response
that needs it.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

#: `connect-src 'self'` covers same-origin WebSocket in every current
#: browser; no `ws:` scheme is listed because that would allow any host.
CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self'; frame-ancestors 'none'"
)

_PAGE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    # The shell is tiny and the assets it names are content-hashed; the
    # page itself must never be cached or a deploy leaves stale hashes.
    "Cache-Control": "no-store",
}


def default_dist_dir() -> Path:
    """Where the Vite build lands, inside the installed package."""
    return Path(str(resources.files("yfin.ui") / "static" / "dist"))


def install_pages(app: FastAPI, dist_dir: Path) -> None:
    index = (dist_dir / "index.html").read_text(encoding="utf-8")
    pages = APIRouter(include_in_schema=False)

    # `path` is unused but must be declared: FastAPI binds `{path:path}`
    # to it. On /ui and /ui/ the default applies (verified: no 422).
    def spa(path: str = "") -> HTMLResponse:
        return HTMLResponse(index, headers=_PAGE_HEADERS)

    pages.add_api_route("/ui", spa, methods=["GET"])
    pages.add_api_route("/ui/", spa, methods=["GET"])
    pages.add_api_route("/ui/t/{path:path}", spa, methods=["GET"])

    app.include_router(pages)
    app.mount("/ui/assets", StaticFiles(directory=dist_dir / "assets"), name="ui-assets")
```

`src/yfin/ui/__init__.py`'ye docstring'in altına:

```python
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from yfin.api.core.config import ApiSettings
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)


def install(app: FastAPI, settings: ApiSettings, dist_dir: Path | None = None) -> None:
    """Mounts the UI. Order matters: the API router, with its catch-all
    404, goes in before the pages so /ui/api/* can never fall through to
    index.html."""
    from yfin.ui import pages, router

    app.include_router(router.router)

    dist = dist_dir if dist_dir is not None else pages.default_dist_dir()
    # Both halves, because StaticFiles raises in its constructor when the
    # directory is absent: a build with index.html but no assets/ would
    # otherwise take the API down at startup.
    if not (dist / "index.html").is_file() or not (dist / "assets").is_dir():
        # A checkout without `npm run build`: the API and its /ui/api
        # routes work, the page does not, and the log says why.
        log.warning("ui_build_missing", dist=str(dist))
        return
    pages.install_pages(app, dist)
```

`src/yfin/api/app.py`, `create_app` içinde `app.include_router(datasets.router)` satırından sonra, `openapi_document.install(app)` satırından önce:

```python
    if settings.ui_enabled:
        # Validated here, not at field level, so `yfin api client` and the
        # health probe still run with a misconfigured UI. Imported here,
        # not at module level, so a deployment with the UI off never
        # loads it.
        settings.validate_ui()
        from yfin.ui import install as install_ui

        install_ui(app, settings)
```

`pyproject.toml`, mevcut `[tool.setuptools.package-data]` bloğundaki `"yfin.api.core"` satırının altına şu satırları **ekle** (mevcut satır kalır):

```toml
# The Vite build. Gitignored (`dist/`) and produced by `npm run build` in
# web/; without it a wheel serves /ui/api but no page, and logs why.
"yfin.ui" = ["static/dist/**"]
```

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `uv run pytest tests/unit/test_ui_pages.py tests/unit/test_api_contract.py tests/unit/test_api_app.py -v`
Expected: PASS. `test_create_app_leaves_ui_out_when_disabled` için `/ui` 404'ü mevcut `_http_exception` handler'ından gelir.

- [ ] **Step 5: Lint, tip, kontrat**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`

- [ ] **Step 6: Commit**

```bash
git add src/yfin/ui/pages.py src/yfin/ui/__init__.py src/yfin/api/app.py pyproject.toml tests/unit/test_ui_pages.py
git commit -m "feat(ui): serve the SPA under /ui by hand, mounted when enabled

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---
### Task 7: `web/` iskeleti: Vite, TypeScript, vitest, eslint, API istemcisi

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/eslint.config.js`, `web/index.html`, `web/src/vite-env.d.ts`, `web/src/test/setup.ts`
- Create: `web/src/api/client.ts`
- Modify: `.gitignore` (`web/node_modules/`)
- Test: `web/src/api/client.test.ts`

**Interfaces:**
- Produces (`web/src/api/client.ts`):
  - `class UnauthorizedError extends Error`; `class ApiError extends Error { status: number; type: string }`
  - `apiFetch<T>(path: string, init?: RequestInit): Promise<T>` — `cache: "no-store"`, `credentials: "same-origin"`, 401 → `UnauthorizedError`, diğer !ok → `ApiError`, 204 → `undefined`
  - `getMe(): Promise<Me>`; `login(password: string): Promise<void>`; `logout(): Promise<void>`; `getSymbol(symbol: string): Promise<SymbolDetail>`
  - `interface Me { authenticated: boolean; expires_at: number | null; live_enabled: boolean }`
  - `interface SymbolDetail { symbol: string; short_name: string | null; long_name: string | null; exchange: string | null; full_exchange_name: string | null; currency: string | null; quote_type: string | null; timezone: string | null; is_active: boolean; info: Record<string, unknown> | null }`

- [ ] **Step 1: Paket iskeletini oluştur**

```json
// web/package.json
{
  "name": "yfin-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "check": "tsc --noEmit",
    "lint": "eslint src",
    "test": "vitest run"
  },
  "dependencies": {
    "react": "^19.2.0",
    "react-dom": "^19.2.0",
    "react-router": "^7.9.0"
  },
  "devDependencies": {
    "@eslint/js": "^9.30.0",
    "@testing-library/dom": "^10.4.0",
    "@testing-library/jest-dom": "^6.6.0",
    "@testing-library/react": "^16.3.0",
    "@testing-library/user-event": "^14.6.0",
    "@types/react": "^19.2.0",
    "@types/react-dom": "^19.2.0",
    "@vitejs/plugin-react": "^5.2.0",
    "eslint": "^9.30.0",
    "eslint-plugin-react-hooks": "^6.0.0",
    "jsdom": "^26.0.0",
    "typescript": "^5.9.0",
    "typescript-eslint": "^8.40.0",
    "vite": "^8.0.0",
    "vitest": "^4.1.0"
  }
}
```

```ts
// web/vite.config.ts
/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the API process under /ui; the build lands inside the Python
// package so a wheel carries it (pyproject: package-data "yfin.ui").
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "../src/yfin/ui/static/dist",
    emptyOutDir: true,
  },
  server: {
    // Same origin for the cookie: the dev server forwards everything the
    // page calls to the API on :8000.
    proxy: {
      "/ui/api": "http://localhost:8000",
      "/ui/ws": { target: "ws://localhost:8000", ws: true },
      "/v1": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    globals: false,
  },
});
```

```json
// web/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noEmit": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "types": ["vite/client"]
  },
  "include": ["src", "vite.config.ts"]
}
```

```js
// web/eslint.config.js
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    // Named explicitly rather than spreading `recommended`: the plugin's
    // 6.x preset also carries React Compiler rules whose verdicts on this
    // code were not checked, and a lint gate must be deterministic.
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
);
```

```html
<!-- web/index.html -->
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>yfin terminal</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/app/main.tsx"></script>
  </body>
</html>
```

```ts
// web/src/vite-env.d.ts
/// <reference types="vite/client" />
```

```ts
// web/src/test/setup.ts
import "@testing-library/jest-dom/vitest";
```

Kök `.gitignore` sonuna `web/node_modules/` satırını ekle (bu task'ın `git add`'inde yer alır).

Run: `cd web && npm install`
Expected: `node_modules/` ve `package-lock.json` oluşur; `git status` `node_modules`'ı göstermez. `package-lock.json` commit edilir. Sürüm gerekçesi: vitest 3.x Vite 7'ye bağlıdır, Vite 8 için vitest 4; `@vitejs/plugin-react` yalnız 5.2+ Vite 8 peer'ı taşır. npm `ERESOLVE` verirse önce bu üçünün güncel majör'lerini kontrol et, lock'u ondan sonra commit'le.

- [ ] **Step 2: Failing test'i yaz**

```ts
// web/src/api/client.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, UnauthorizedError, apiFetch, getMe, getSymbol, login } from "./client";

const PW = "hunter2";

function respond(status: number, body: unknown, contentType = "application/json"): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": contentType },
  });
}

describe("apiFetch", () => {
  afterEach(() => vi.restoreAllMocks());

  it("sends same-origin credentials and never caches", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(respond(200, { ok: 1 }));
    await apiFetch("/ui/api/me");
    const [, init] = spy.mock.calls[0]!;
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
  });

  it("turns a 401 into UnauthorizedError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(401, { type: "unauthenticated", title: "x" }, "application/problem+json"),
    );
    await expect(apiFetch("/v1/symbols/AAPL")).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("turns another problem into ApiError with the type", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(404, { type: "not_found", title: "No such symbol" }, "application/problem+json"),
    );
    const err = await apiFetch("/v1/symbols/NOPE").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).type).toBe("not_found");
  });

  it("returns undefined for a 204", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiFetch("/ui/api/logout", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("endpoints", () => {
  afterEach(() => vi.restoreAllMocks());

  it("getMe reads /ui/api/me", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(200, { authenticated: false, expires_at: null, live_enabled: false }),
    );
    const me = await getMe();
    expect(spy.mock.calls[0]![0]).toBe("/ui/api/me");
    expect(me.authenticated).toBe(false);
  });

  it("login posts the form field", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await login(PW);
    const [url, init] = spy.mock.calls[0]!;
    expect(url).toBe("/ui/api/login");
    expect(init?.method).toBe("POST");
    expect(String(init?.body)).toContain(`password=${PW}`);
  });

  it("getSymbol upper-cases and unwraps the resource envelope", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond(200, { data: { symbol: "AAPL", is_active: true, info: null } }),
    );
    const detail = await getSymbol("aapl");
    expect(spy.mock.calls[0]![0]).toBe("/v1/symbols/AAPL");
    expect(detail.symbol).toBe("AAPL");
  });
});
```

Run: `cd web && npm test`
Expected: FAIL, `./client` modülü bulunamaz.

- [ ] **Step 3: İstemciyi yaz**

Önce `/v1/symbols/{s}` zarfını doğrula: `src/yfin/api/schemas/common.py`'de `Resource[T]` `{"data": T}` mi? Farklıysa `getSymbol` içindeki `data` alanını ve bu task'ın testini, ayrıca Task 8 (`DES.test.tsx`) ve Task 9 (`App.test.tsx` `symbolBody`) mock gövdelerini ona göre değiştir.

```ts
// web/src/api/client.ts
// One fetch wrapper for the page. Same-origin cookie, never cached: the
// API's Vary header names Authorization, not Cookie, so a cached /v1
// response could outlive a logout.

export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
    this.name = "UnauthorizedError";
  }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly type: string,
    title: string,
  ) {
    super(title);
    this.name = "ApiError";
  }
}

interface Problem {
  type?: string;
  title?: string;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) {
    let problem: Problem = {};
    try {
      problem = (await response.json()) as Problem;
    } catch {
      // A non-JSON error body carries nothing worth showing.
    }
    throw new ApiError(response.status, problem.type ?? "unknown", problem.title ?? response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface Me {
  authenticated: boolean;
  expires_at: number | null;
  live_enabled: boolean;
}

export interface SymbolDetail {
  symbol: string;
  short_name: string | null;
  long_name: string | null;
  exchange: string | null;
  full_exchange_name: string | null;
  currency: string | null;
  quote_type: string | null;
  timezone: string | null;
  is_active: boolean;
  info: Record<string, unknown> | null;
}

export function getMe(): Promise<Me> {
  return apiFetch<Me>("/ui/api/me");
}

export async function login(password: string): Promise<void> {
  const body = new URLSearchParams({ password });
  await apiFetch<void>("/ui/api/login", { method: "POST", body });
}

export async function logout(): Promise<void> {
  await apiFetch<void>("/ui/api/logout", { method: "POST" });
}

export async function getSymbol(symbol: string): Promise<SymbolDetail> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const envelope = await apiFetch<{ data: SymbolDetail }>(`/v1/symbols/${code}`);
  return envelope.data;
}
```

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `cd web && npm test && npm run check && npm run lint`
Expected: 7 test PASS, tsc ve eslint temiz.

- [ ] **Step 5: Commit**

```bash
git add .gitignore web/package.json web/package-lock.json web/vite.config.ts web/tsconfig.json web/eslint.config.js web/index.html web/src/vite-env.d.ts web/src/test/setup.ts web/src/api/client.ts web/src/api/client.test.ts
git commit -m "feat(web): Vite + React + TypeScript scaffold and the API client

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 8: `DES` paneli

**Files:**
- Create: `web/src/app/session.tsx`
- Create: `web/src/panels/DES.tsx`
- Test: `web/src/panels/DES.test.tsx`

**Interfaces:**
- Produces:
  - `SessionProvider` + `useSession(): { me: Me | null; refresh(): Promise<void>; requireLogin(): void }` (`web/src/app/session.tsx`)
  - `DES: React.FC<{ symbol: string }>`: `/v1/symbols/{s}`'den kimlik alanları ve `info` içinden `sector`, `industry`, `marketCap`, `trailingPE`, `forwardPE`, `dividendYield`, `beta`, `fiftyTwoWeekLow`, `fiftyTwoWeekHigh`, `website`; 401'de `requireLogin()`; 404'te "No such symbol"; diğer hatalarda "Could not load" + Retry düğmesi. Yükleme yalnız oturum doğrulanmışken başlar ve oturum `false`→`true` olunca yeniden koşar (spec: "başarılı login sonrası son komut yeniden koşar"). Inline `style=` kullanılmaz (CSP `style-src 'self'`).
  - `formatBig(value: number): string` (3.5e12 → "3.50T")
- Consumes: Task 7 `getSymbol`, `getMe`, `ApiError`, `UnauthorizedError`, `Me`.

- [ ] **Step 1: Failing test'i yaz**

```tsx
// web/src/panels/DES.test.tsx
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DES, formatBig } from "./DES";
import { SessionProvider } from "../app/session";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const me = { authenticated: true, expires_at: 1, live_enabled: false };

afterEach(() => vi.restoreAllMocks());

function renderDES(symbol: string) {
  return render(
    <SessionProvider>
      <DES symbol={symbol} />
    </SessionProvider>,
  );
}

describe("formatBig", () => {
  it("scales to K/M/B/T with two decimals", () => {
    expect(formatBig(3_500_000_000_000)).toBe("3.50T");
    expect(formatBig(12_345_678)).toBe("12.35M");
    expect(formatBig(999)).toBe("999");
  });
});

describe("DES", () => {
  it("renders identity and the info fields it knows", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      if (url === "/v1/symbols/AAPL") return json(200, { data: {
        symbol: "AAPL", long_name: "Apple Inc.", short_name: "Apple", exchange: "NMS",
        full_exchange_name: "NasdaqGS", currency: "USD", quote_type: "EQUITY",
        timezone: "America/New_York", is_active: true,
        info: { sector: "Technology", industry: "Consumer Electronics", marketCap: 3500000000000, trailingPE: 33.1, website: "https://apple.com" },
      } });
      throw new Error(`unexpected ${url}`);
    });
    renderDES("AAPL");
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(screen.getByText("NasdaqGS")).toBeInTheDocument();
    expect(screen.getByText("Technology")).toBeInTheDocument();
    expect(screen.getByText("3.50T")).toBeInTheDocument();
    expect(screen.getByText("33.10")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
  });

  it("says so when the symbol does not exist", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      return new Response(JSON.stringify({ type: "not_found", title: "No such symbol" }), {
        status: 404, headers: { "content-type": "application/problem+json" },
      });
    });
    renderDES("NOPE");
    expect(await screen.findByText(/No such symbol/)).toBeInTheDocument();
  });

  it("shows a retry on a server error", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, me);
      return new Response("{}", { status: 500, headers: { "content-type": "application/problem+json" } });
    });
    renderDES("AAPL");
    expect(await screen.findByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("waits for a session and loads once it is there", async () => {
    let authed = false;
    let symbolCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/ui/api/me") return json(200, { ...me, authenticated: authed });
      if (url === "/v1/symbols/AAPL") {
        symbolCalls += 1;
        if (!authed) return new Response("{}", { status: 401, headers: { "content-type": "application/problem+json" } });
        return json(200, { data: { symbol: "AAPL", long_name: "Apple Inc.", short_name: null, exchange: null, full_exchange_name: null, currency: null, quote_type: null, timezone: null, is_active: true, info: null } });
      }
      throw new Error(`unexpected ${url}`);
    });
    render(
      <SessionProvider>
        <SessionProbe />
        <DES symbol="AAPL" />
      </SessionProvider>,
    );
    // /me says authenticated:false, so DES must not even try.
    await screen.findByText("session:false");
    expect(symbolCalls).toBe(0);
    authed = true;
    await userEvent.click(screen.getByRole("button", { name: "refresh" }));
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
    expect(symbolCalls).toBe(1);
  });
});

// A tiny consumer of useSession so the test can flip the session.
function SessionProbe() {
  const { me, refresh } = useSession();
  return (
    <>
      <span>session:{String(me?.authenticated ?? "null")}</span>
      <button onClick={() => void refresh()}>refresh</button>
    </>
  );
}
```

Dosyanın import'ları: `import userEvent from "@testing-library/user-event";` ve `import { SessionProvider, useSession } from "../app/session";` (yalnız `SessionProvider` import eden satırın yerine).

Run: `cd web && npm test`
Expected: FAIL, `./DES` ve `../app/session` bulunamaz.

- [ ] **Step 2: Oturum sağlayıcısını yaz**

```tsx
// web/src/app/session.tsx
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { getMe, type Me } from "../api/client";

interface SessionState {
  me: Me | null; // null until /ui/api/me has answered once
  refresh: () => Promise<void>;
  requireLogin: () => void;
}

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);

  const refresh = useCallback(async () => {
    setMe(await getMe());
  }, []);

  // A 401 anywhere on the page flips the session to "not authenticated"
  // so the modal appears; the caller retries after login.
  const requireLogin = useCallback(() => {
    setMe({ authenticated: false, expires_at: null, live_enabled: false });
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(() => ({ me, refresh, requireLogin }), [me, refresh, requireLogin]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const value = useContext(SessionContext);
  if (value === null) throw new Error("useSession outside SessionProvider");
  return value;
}
```

- [ ] **Step 3: Paneli yaz**

```tsx
// web/src/panels/DES.tsx
import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, UnauthorizedError, getSymbol, type SymbolDetail } from "../api/client";
import { useSession } from "../app/session";

type State =
  | { kind: "loading" }
  | { kind: "ready"; detail: SymbolDetail }
  | { kind: "missing" }
  | { kind: "error" };

type Kind = "text" | "big" | "num" | "pct";

const INFO_ROWS: ReadonlyArray<[key: string, label: string, kind: Kind]> = [
  ["sector", "Sector", "text"],
  ["industry", "Industry", "text"],
  ["marketCap", "Market cap", "big"],
  ["trailingPE", "P/E (ttm)", "num"],
  ["forwardPE", "P/E (fwd)", "num"],
  ["dividendYield", "Dividend yield", "pct"],
  ["beta", "Beta", "num"],
  ["fiftyTwoWeekLow", "52w low", "num"],
  ["fiftyTwoWeekHigh", "52w high", "num"],
  ["website", "Website", "text"],
];

export function formatBig(value: number): string {
  const units: Array<[number, string]> = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, suffix] of units) {
    if (Math.abs(value) >= size) return `${(value / size).toFixed(2)}${suffix}`;
  }
  return value.toFixed(0);
}

function format(value: unknown, kind: Kind): string | null {
  if (value === null || value === undefined) return null;
  if (kind === "text") return String(value);
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  if (kind === "big") return formatBig(value);
  if (kind === "pct") return `${(value * 100).toFixed(2)}%`;
  return value.toFixed(2);
}

export function DES({ symbol }: { symbol: string }) {
  const { me, requireLogin } = useSession();
  const authenticated = me?.authenticated === true;
  const [state, setState] = useState<State>({ kind: "loading" });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", detail: await getSymbol(symbol) });
    } catch (err) {
      if (err instanceof UnauthorizedError) requireLogin();
      else if (err instanceof ApiError && err.status === 404) setState({ kind: "missing" });
      else setState({ kind: "error" });
    }
  }, [symbol, requireLogin]);

  // Runs when the symbol changes AND when the session comes back after a
  // login: the spec's "the last command re-runs after a successful login".
  useEffect(() => {
    if (authenticated) void load();
  }, [load, authenticated]);

  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <p className="error">No such symbol: {symbol}</p>;
  if (state.kind === "error")
    return (
      <p className="error">
        Could not load {symbol}. <button onClick={() => void load()}>Retry</button>
      </p>
    );

  const d = state.detail;
  const info = d.info ?? {};
  return (
    <section>
      <h2>{d.long_name ?? d.short_name ?? d.symbol}</h2>
      <dl className="des">
        <dt>Symbol</dt><dd>{d.symbol}</dd>
        <dt>Exchange</dt><dd>{d.full_exchange_name ?? d.exchange ?? "—"}</dd>
        <dt>Type</dt><dd>{d.quote_type ?? "—"}</dd>
        <dt>Currency</dt><dd>{d.currency ?? "—"}</dd>
        <dt>Timezone</dt><dd>{d.timezone ?? "—"}</dd>
        {INFO_ROWS.map(([key, label, kind]) => {
          const text = format(info[key], kind);
          if (text === null) return null;
          // Fragment, not a wrapper: <dl> only allows dt/dd children, and
          // an inline style= would be blocked by the page's CSP anyway.
          return (
            <Fragment key={key}>
              <dt>{label}</dt><dd>{text}</dd>
            </Fragment>
          );
        })}
      </dl>
      {d.info === null && <p className="muted">Never synced: run yfin sync --symbols {d.symbol}</p>}
    </section>
  );
}
```

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `cd web && npm test && npm run check && npm run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/app/session.tsx web/src/panels
git commit -m "feat(web): session provider and the DES panel

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 9: Uygulama kabuğu: router, login modalı, sembol kutusu

**Files:**
- Create: `web/src/app/main.tsx`, `web/src/app/App.tsx`, `web/src/app/Shell.tsx`, `web/src/app/LoginModal.tsx`, `web/src/app/styles.css`
- Test: `web/src/app/App.test.tsx`

**Interfaces:**
- Produces:
  - `AppRoutes` (BrowserRouter dışında; test MemoryRouter ile sarar)
  - Rotalar: `/ui` → `localStorage["yfin.ui.last"]` varsa oraya, yoksa `/ui/t/-/DES`; `/ui/t/:symbol/:code` → `Shell`; diğer her şey `/ui`
  - `Shell`: üstte tek satır input (`aria-label="command"`), Enter'da metni büyük harfe çevirip `/ui/t/{SYMBOL}/DES`'e gider (1b bunu parser ile değiştirir); sembol varsa şerit (`.strip`) sembol kodunu gösterir; `code === "DES"` ise `DES` paneli
  - `LAST_KEY = "yfin.ui.last"`
- Consumes: Task 7 `login`, `ApiError`; Task 8 `SessionProvider`, `useSession`, `DES`.

- [ ] **Step 1: Failing test'i yaz**

```tsx
// web/src/app/App.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { AppRoutes } from "./App";

const PW = "hunter2";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function symbolBody(symbol: string, longName: string) {
  return { data: { symbol, long_name: longName, short_name: null, exchange: null, full_exchange_name: null, currency: null, quote_type: null, timezone: null, is_active: true, info: null } };
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => handler(String(input), init));
}

function unauthorized(): Response {
  return new Response("{}", { status: 401, headers: { "content-type": "application/problem+json" } });
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("AppRoutes", () => {
  it("shows the login modal when unauthenticated", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: false, expires_at: null, live_enabled: false });
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    expect(await screen.findByLabelText("password")).toBeInTheDocument();
  });

  it("logs in and reveals the shell with the symbol", async () => {
    let authed = false;
    mockFetch((url, init) => {
      if (url === "/ui/api/me") return json(200, { authenticated: authed, expires_at: authed ? 1 : null, live_enabled: false });
      if (url === "/ui/api/login" && init?.method === "POST") {
        authed = true;
        return new Response(null, { status: 204 });
      }
      // Like the real API: no session, no data. This is what proves DES
      // loads AFTER login rather than before the modal appeared.
      if (url === "/v1/symbols/AAPL") return authed ? json(200, symbolBody("AAPL", "Apple Inc.")) : unauthorized();
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await userEvent.type(await screen.findByLabelText("password"), `${PW}{enter}`);
    await waitFor(() => expect(screen.queryByLabelText("password")).not.toBeInTheDocument());
    expect(await screen.findByText("Apple Inc.")).toBeInTheDocument();
  });

  it("navigates to DES of the typed symbol on Enter", async () => {
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url.startsWith("/v1/symbols/")) {
        const symbol = url.split("/").pop()!;
        return json(200, symbolBody(symbol, `${symbol} Corp`));
      }
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui/t/AAPL/DES");
    await userEvent.type(await screen.findByLabelText("command"), "msft{enter}");
    expect(await screen.findByText("MSFT Corp")).toBeInTheDocument();
  });

  it("redirects /ui to the last visited triple", async () => {
    localStorage.setItem("yfin.ui.last", "/ui/t/TSLA/DES");
    mockFetch((url) => {
      if (url === "/ui/api/me") return json(200, { authenticated: true, expires_at: 1, live_enabled: false });
      if (url === "/v1/symbols/TSLA") return json(200, symbolBody("TSLA", "Tesla"));
      throw new Error(`unexpected ${url}`);
    });
    mount("/ui");
    expect(await screen.findByText("Tesla")).toBeInTheDocument();
  });
});
```

Run: `cd web && npm test`
Expected: FAIL, `./App` bulunamaz.

- [ ] **Step 2: Login modalını yaz**

```tsx
// web/src/app/LoginModal.tsx
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, login } from "../api/client";
import { useSession } from "./session";

export function LoginModal() {
  const { refresh } = useSession();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(draft);
      setDraft("");
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) setError("Too many attempts. Wait a minute.");
      else setError("Wrong password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="login">
      <form className="modal" onSubmit={submit}>
        <h1>yfin terminal</h1>
        <label>
          Password
          <input
            aria-label="password"
            type="password"
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={busy}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy || draft === ""}>Sign in</button>
      </form>
    </div>
  );
}
```

- [ ] **Step 3: Kabuğu ve rotaları yaz**

```tsx
// web/src/app/Shell.tsx
import { useEffect, useState } from "react";
import type { KeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router";
import { DES } from "../panels/DES";

export const LAST_KEY = "yfin.ui.last";

export function Shell() {
  const { symbol = "-", code = "DES" } = useParams();
  const navigate = useNavigate();
  const [draft, setDraft] = useState("");

  useEffect(() => {
    try {
      localStorage.setItem(LAST_KEY, `/ui/t/${symbol}/${code}`);
    } catch {
      // Private mode or blocked storage: the redirect just loses its hint.
    }
  }, [symbol, code]);

  // 1a: a plain symbol box. 1b replaces this with the command parser.
  function onKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    const next = draft.trim().toUpperCase();
    if (next === "") return;
    setDraft("");
    void navigate(`/ui/t/${encodeURIComponent(next)}/DES`);
  }

  const hasSymbol = symbol !== "-";
  return (
    <div className="shell">
      <header className="command-bar">
        <input
          aria-label="command"
          placeholder="Symbol, then Enter"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          autoFocus
        />
      </header>
      {hasSymbol && (
        <div className="strip">
          <span className="strip-symbol">{symbol}</span>
        </div>
      )}
      <main className="panel">
        {code === "DES" ? (
          hasSymbol ? <DES symbol={symbol} /> : <p className="muted">Type a symbol to begin.</p>
        ) : (
          <p className="muted">Unknown function {code}.</p>
        )}
      </main>
    </div>
  );
}
```

```tsx
// web/src/app/App.tsx
import { Navigate, Route, Routes } from "react-router";
import { LoginModal } from "./LoginModal";
import { LAST_KEY, Shell } from "./Shell";
import { SessionProvider, useSession } from "./session";

function RootRedirect() {
  let last: string | null = null;
  try {
    last = localStorage.getItem(LAST_KEY);
  } catch {
    last = null;
  }
  return <Navigate to={last && last.startsWith("/ui/t/") ? last : "/ui/t/-/DES"} replace />;
}

function Gate() {
  const { me } = useSession();
  if (me === null) return <p className="muted">Connecting…</p>;
  return (
    <>
      <Routes>
        <Route path="/ui" element={<RootRedirect />} />
        <Route path="/ui/t/:symbol/:code" element={<Shell />} />
        <Route path="*" element={<Navigate to="/ui" replace />} />
      </Routes>
      {!me.authenticated && <LoginModal />}
    </>
  );
}

export function AppRoutes() {
  return (
    <SessionProvider>
      <Gate />
    </SessionProvider>
  );
}
```

```tsx
// web/src/app/main.tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { AppRoutes } from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  </StrictMode>,
);
```

```css
/* web/src/app/styles.css */
:root { color-scheme: dark; --bg: #0b0e11; --fg: #d7dde3; --muted: #7f8a96; --line: #1f262e; --accent: #f2b544; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 13px/1.4 ui-monospace, "SF Mono", Menlo, monospace; }
.shell { display: flex; flex-direction: column; height: 100vh; }
.command-bar { border-bottom: 1px solid var(--line); padding: 6px 10px; }
.command-bar input { width: 100%; background: transparent; border: 0; color: var(--accent); font: inherit; outline: none; text-transform: uppercase; }
.strip { display: flex; gap: 16px; padding: 6px 10px; border-bottom: 1px solid var(--line); }
.strip-symbol { color: var(--accent); font-weight: 600; }
.panel { flex: 1; overflow: auto; padding: 10px; }
.muted { color: var(--muted); }
.error { color: #ff6b6b; }
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: grid; place-items: center; }
.modal { background: #12171d; border: 1px solid var(--line); padding: 20px 24px; display: grid; gap: 10px; min-width: 280px; }
.modal input { font: inherit; padding: 6px; background: var(--bg); color: var(--fg); border: 1px solid var(--line); }
.modal button { font: inherit; padding: 6px 10px; background: var(--accent); color: #000; border: 0; cursor: pointer; }
dl.des { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; }
dl.des dt { color: var(--muted); }
```

- [ ] **Step 4: Testlerin geçtiğini doğrula**

Run: `cd web && npm test && npm run check && npm run lint`
Expected: PASS.

- [ ] **Step 5: Gerçek build ve elle uçtan uca doğrulama**

Run: `cd web && npm run build && ls ../src/yfin/ui/static/dist`
Expected: `index.html` ve `assets/`; `git status` bunları göstermez.

`.env` dosyasına dokunma. `docker compose up -d timescaledb redis`; ardından değerleri komut satırında ver:

```bash
YFAPI_UI_ENABLED=true YFAPI_UI_PASSWORD=<bir değer seç> uv run uvicorn yfin.api.app:app --port 8000
```

Tarayıcıda `http://localhost:8000/ui` → login modalı → şifre → `AAPL` yaz, Enter → DES görünür; tarayıcı konsolunda CSP ihlali yok. `http://localhost:8000/v1/typo` → problem JSON, HTML değil. Gözlemi Step 6'daki commit mesajının gövdesine yaz. Docker ya da tarayıcı yoksa "koşulmadı" diye raporla.

- [ ] **Step 6: Commit**

```bash
git add web/src/app
git commit -m "feat(web): app shell, session gate, login modal and symbol box

<Step 5 gözlemi: tarayıcı, login → AAPL DES görüldü, /v1/typo → problem JSON, CSP ihlali yok/var; ya da koşulmadı>

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 10: CI `web` job'u, Dockerfile node aşaması, compose, README

**Files:**
- Modify: `.github/workflows/ci.yml` (yeni `web` job'u; `needs` yok, `check` ve `repo` ile paralel koşar)
- Create: `.dockerignore`
- Modify: `Dockerfile` (yeni build aşaması, runtime COPY)
- Modify: `docker-compose.yml:87-98`
- Modify: `README.md` ("Planned" tablosu, "Common commands" altı, "Layout" tablosu)

- [ ] **Step 1: CI job'unu ekle**

```yaml
  # The browser UI. Its own job on purpose: a Node failure must not mask
  # a Python one, and the Python jobs must not need Node -- the API runs
  # without the build and says so in the log.
  web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: npm ci
      - run: npm run check
      - run: npm run lint
      - run: npm test
      - run: npm run build
```

- [ ] **Step 2: `.dockerignore` ve Dockerfile node aşaması**

Depoda `.dockerignore` yok; `COPY web ./` host'taki macOS `node_modules`'ını, `COPY src` ise yerel `static/dist`'i imaja taşırdı. Oluştur:

```
.git
.venv
.env
web/node_modules
src/yfin/ui/static/dist
.claude
.superpowers
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
```

`FROM python:3.13-slim-bookworm AS runtime` satırından önce:

```dockerfile
# The browser UI. Built here so the runtime image needs no Node; the
# output lands where the Python package expects it (pyproject:
# package-data "yfin.ui" = static/dist/**).
FROM node:22-slim AS web

WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
# vite.config.ts writes to ../src/yfin/ui/static/dist, i.e. /app/src/...
RUN npm run build
```

Runtime aşamasında `COPY --from=builder --chown=yfin:yfin /app/src /app/src` satırından sonra:

```dockerfile
COPY --from=web --chown=yfin:yfin /app/src/yfin/ui/static/dist /app/src/yfin/ui/static/dist
```

Run: `docker build -t yfin-api:ui .`
Expected: başarılı; `docker run --rm yfin-api:ui ls /app/src/yfin/ui/static/dist` `index.html` ve `assets` gösterir. Docker yerelde yoksa bu adımı "koşulmadı" diye rapor et; CI `web` job'u build'i zaten doğrular.

- [ ] **Step 3: compose**

`docker-compose.yml` `api.environment` bloğuna `YFAPI_DOCS_ENABLED` satırından sonra:

```yaml
      # The browser UI under /ui. Off unless both are set; with the UI on
      # and the password empty the API refuses to start.
      YFAPI_UI_ENABLED: ${YFAPI_UI_ENABLED:-false}
      YFAPI_UI_PASSWORD: ${YFAPI_UI_PASSWORD:-}
      # Needed for the Secure cookie flag and, in 1c, the WebSocket
      # Origin check. Leave empty on plain-HTTP localhost.
      YFAPI_PUBLIC_BASE_URL: ${YFAPI_PUBLIC_BASE_URL:-}
```

- [ ] **Step 4: README**

"Planned" tablosuna satır:

```markdown
| **Web terminal** | **In progress** | Keyboard-first browser UI under `/ui`, served by the API process. Phase 1a ships login and `DES`; the command language, live ticks and charts follow (`docs/superpowers/specs/2026-09-07-web-terminal-design.md`). |
```

"Common commands" bölümünün altına:

```markdown
### Web terminal

    cd web && npm ci && npm run build   # writes src/yfin/ui/static/dist
    YFAPI_UI_ENABLED=true YFAPI_UI_PASSWORD=<choose one> \
      uvicorn yfin.api.app:app --port 8000
    open http://localhost:8000/ui

One password, one operator. Behind a reverse proxy set
`YFAPI_TRUSTED_PROXIES`, or every login attempt in the world shares one
rate-limit bucket. Set `YFAPI_PUBLIC_BASE_URL` to the `https://` origin
so the session cookie is marked `Secure`; with it empty the cookie
travels over plain HTTP, which is acceptable on localhost and nowhere
else.
```

(README'de kod blokları ```` ```bash ```` ile çevrili; yukarıdaki girintili blok da öyle yazılır.)

"Layout" tablosuna (`| Path | Contents |`) iki satır:

```markdown
| `src/yfin/ui/` | Web terminal: session cookie, `/ui/api` routes, SPA pages |
| `web/` | The SPA source (React + Vite); builds into `src/yfin/ui/static/dist` |
```

- [ ] **Step 5: Tam doğrulama**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check && (cd web && npm run check && npm run lint && npm test && npm run build)`
Expected: hepsi yeşil; `git status` `dist` göstermez.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml .dockerignore Dockerfile docker-compose.yml README.md
git commit -m "build(ui): web CI job, Node build stage, dockerignore, compose env, README

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

## Self-review notları

- **Spec kapsamı (1a):** ayarlar ve `.env.example` (T1), `FixedWindow` taşıma (T2), çerez JWT (T3), login/logout/me/catch-all (T4), çerez dalı ve `meter` atlaması (T5), sayfalar/CSP/montaj/package-data (T6), Vite iskeleti, dev proxy ve `.gitignore` (T7), oturum sağlayıcı ve DES (T8), kabuk/login modalı/`localStorage` yönlendirme (T9), CI/Dockerfile/compose/README (T10). "UI kapalıyken import edilmez" ve "`openapi.json` değişmez" garantileri T6 testlerinde. `live_enabled` 1c'ye kadar `false`.
- **Tip tutarlılığı:** `session.issue -> (str, int)`, `verify -> UiClaims(jti, expires_at)`; `Me.expires_at: int | null` Python ve TS'te aynı; `getSymbol` `{data: SymbolDetail}` zarfını açar (T7 Step 3 doğrulama notu); `UI_CLIENT_ID`/`UI_PAGE_CAP` T5'te tanımlanır ve aynı adla kullanılır; `LAST_KEY` T9'da tanımlanır, T9 testi aynı dizeyi kullanır.
- **İnceleme sonrası düzeltmeler (2026-09-07):** `guard()` test rotasında `Depends` ile; vitest 4 / plugin-react 5.2 / `@testing-library/dom`; DES yalnız oturum varken yükler ve login sonrası yeniden koşar; inline stil yerine `Fragment`; `.dockerignore`; testlerde `_env_file=None` ve `kid`/`iss` pinleme; `UiSession` probe testleri; `HEAD`/`OPTIONS` catch-all; `assets/` kontrolü; `yfin.ui` import edilmemesi için subprocess testi; `Me.expires_at` epoch (spec güncellendi); eslint kuralları açık liste.
