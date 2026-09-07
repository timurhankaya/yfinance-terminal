# Web Terminal 1b: Komut Dili ve REST Panelleri — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Komut kutusuna `AAPL FA`, `ANR`, `N`, `CF 10-K`, `HELP` yazılabilen, sembolü doğrulayan, Esc ile geçmişte gezen ve dört REST panelini gösteren terminal.

**Architecture:** Komut dili saf TypeScript modüllerinde yaşar (`web/src/commands/`: parser, registry, `PanelSpec`); `Shell` bunları kullanarak URL üçlüsüne (`/ui/t/{SYMBOL}/{CODE}?args`) gider ve react-router history'si gezinme yığınıdır. Paneller `PanelSpec` olarak kaydedilir; `Shell` kayıtlı panelin şablonuna (`single` / `headed`) göre çizer. Veri yükleme tek bir hook'ta (`usePanelData`) toplanır: oturum yokken beklemek, login sonrası yeniden koşmak, eski yanıtı atmak, 401/404/boş/hata durumları. Backend'e yalnızca bir UI rotası eklenir: `/ui/api/symbols/{s}/news` (`news_symbols ⋈ news`), çünkü generic dataset yüzeyi join yapmaz. Diğer paneller mevcut `/v1` rotalarını çerezle çağırır.

**Tech Stack:** React 19, react-router 7, TypeScript (strict, `noUncheckedIndexedAccess`), vitest + RTL, `cmdk` 1.1 (yeni bağımlılık); Python 3.13 / FastAPI / SQLAlchemy 2.

**Spec:** `docs/superpowers/specs/2026-09-07-web-terminal-design.md` — bölümler "Komut dili ve panel modeli", "Hata yönetimi", "Uygulama sırası" (1b satırı), "Testler". Bu plan spec'ten dört yerde bilinçli sapar; Task 8 spec'i güncelletir: (1) argüman token'ları büyük harfe çevrilmez; (2) grammar üç dallıdır (aşağıda); (3) `react-hotkeys-hook`, `@tanstack/react-query` ve `@tanstack/react-table` kullanılmaz — kısayollar tek `keydown` dinleyicisi, veri yükleme `usePanelData` (otomatik yeniden deneme yok, elle Retry), tablo kendi `DataTable`'ımız; (4) palet "yalnız sembol öneki" notunu gösterir (spec'te vardı, 1a'da yoktu).

## Global Constraints

- Committed `openapi.json` **değişmez**: yeni rota `/ui/api` altında ve `include_in_schema=False`. Her task sonunda `uv run python scripts/dump_openapi.py --check`.
- Python: `uv run ruff check . && uv run mypy && uv run pytest -q`. Bilinen taban (ledger): `src/yfin/outbox/kafka.py`'de 3 mypy hatası ve `tests/unit/test_scheduler.py` + `test_schedule_settings.py`'de 12 başarısız test (başka bir oturumun scheduler işi). Kural: **yeni hata/başarısızlık yok**.
- Web: `cd web && npm run check && npm run lint && npm test && npm run build` yeşil, çıktı temiz (`act()` uyarısı yok; async gezinme `await waitFor(...)` ile sarılır). vitest `globals: false`: her test dosyası `afterEach(() => { cleanup(); vi.restoreAllMocks(); })` çağırır. `tsconfig` `noUncheckedIndexedAccess: true`: dizin erişimleri `string | undefined` döner. `src/**/*.test.ts(x)` `npm run check` kapsamındadır.
- Bağımlılık eklerken plain `npm install` çalıştırılmaz (npm 10.9 çöker): `npm install --package-lock-only <pkg>@<sürüm>` sonra `npm ci`.
- Inline `style=` yok (CSP `style-src 'self'`); tüm stil `styles.css`. `cmdk`'nin `Command.Dialog` bileşeni kullanılmaz (radix dialog `<style>` enjekte eder); düz `<Command>` + kendi overlay `div`'imiz. UI metinleri ve yorumlar İngilizce.
- **Grammar (bu planın kuralı; Task 8 spec'i buna göre günceller).** Girdi boşlukla token'lara bölünür. Karar için token'lar büyük harfle karşılaştırılır; **argüman token'ları `parseArgs`'a ham (orijinal harfleriyle) verilir** ve her `parseArgs` kendi normalizasyonunu yapar. Sembol `/^[A-Z0-9.^=-]+$/`.
  1. Tek token, kayıtlı mnemonik → fonksiyon; bağlam sembolü korunur.
  2. Tek token, mnemonik değil → sembol; mevcut panel korunur (panel sembol istemiyorsa `DES`).
  3. İki+ token: ilk ikisi de mnemonikse ilki **sembol** (`CF DES`). İlki mnemonik, ikincisi değilse ilki **fonksiyon**, kalanı arg (`GIP 15m`). Aksi hâlde ilki sembol adayı, ikincisi mnemonik olmalı (`AAPL GIP 1m`), değilse "Unknown function X".
  4. `needsSymbol` ve sembol yoksa "X needs a symbol: type one first, e.g. AAPL X". `parseArgs` hata fırlatırsa mesaj uyarı olarak gösterilir, gezinme olmaz.
- URL: `/ui/t/{SYMBOL}/{CODE}?{args}`; sembolsüz `/ui/t/-/HELP`. `useParams` zaten decode eder; `pathToCommand` decode **etmez**, `commandToPath` `encodeURIComponent` ile encode eder.
- Gezinme: her komut `navigate(path)` (pushState). Esc: komut kutusu odaktaysa kutuyu boşaltıp blur eder, değilse `navigate(-1)`; Shift+Esc `navigate(1)`. Ctrl+K / Meta+K / `/` kutuya odak; `?` HELP; `j`/`k`/`Enter` liste panellerinde. **Genel kural:** bir `INPUT`/`TEXTAREA` odaktayken kısayollar pasiftir; tek istisna komut kutusundaki Esc. Palet açıkken odak paletin input'undadır ve Esc paleti kapatır (cmdk'nin kendi Esc'si), global dinleyici o sırada hiçbir şey yapmaz.
- Sembol doğrulama: `/v1/symbols/{s}`; 404 → kutunun altında "No such symbol X" ve palet `searchSymbols(X)` sonuçlarıyla açılır; hedeflenen fonksiyon (`pendingCode`) saklanır, paletten seçilen sembol o fonksiyonla koşar. Bağlam değişmez.
- 401: panel yüklemeleri login sonrası kendiliğinden yeniden koşar (`usePanelData`); `Shell.submit` sırasında 401 gelirse taslak saklanır ve login sonrası bir kez yeniden gönderilir.
- `/ui/api/symbols/{s}/news`: yalnız çerez (`UiSession`, ilk parametre); `limit` varsayılan 50, en çok 200; `pub_date desc, news_id desc`; yanıt `{"data":[...], "next_cursor": null, "as_of": null}`.
- `page_size_cap` UI için 1000: financials `limit=1000`, dataset `limit=200`, symbols `limit=20`.
- Commit mesajı `Claude-Session:` satırıyla biter; e-posta adresi içeren trailer kullanılmaz.
- Test sabitleri: `KEY = "k" * 32`, `PW = "hunter2"`; `ApiSettings(_env_file=None, jwt_kid="k1", jwt_issuer="yfin-api", ...)`.

---

## Dosya yapısı

| Dosya | Sorumluluk |
| --- | --- |
| `src/yfin/ui/data.py` | UI'a özel okuma rotaları: `/ui/api/symbols/{symbol}/news`; kendi `SessionDep` tanımı |
| `src/yfin/ui/__init__.py` | `install`: `data.router` → `router.router` → pages (docstring güncellenir) |
| `tests/unit/test_ui_data.py`, `tests/repo/test_ui_news_repo.py` | 401/422 birim; join ve sıralama repo |
| `web/src/commands/types.ts` | `PanelArgs`, `PanelProps`, `PanelSpec`, `Command` |
| `web/src/commands/parser.ts` | `parse`, `commandToPath`, `pathToCommand`, `SYMBOL_RE` |
| `web/src/commands/registry.ts` | `registerPanel`, `getPanel`, `listPanels`, `isMnemonic`, `clearRegistry` |
| `web/src/panels/index.ts` | `registerAll()`: `DES`, `HELP` (T3), `FA` (T5), `ANR` (T6), `N`, `CF` (T7) |
| `web/src/panels/common.tsx` | `Loaded<T>`, `usePanelData`, `ErrorCard`, `EmptyCard`, `MissingCard`, `DataTable`, `useListKeys` |
| `web/src/panels/{HELP,FA,ANR,N,CF}.tsx` + testler | paneller |
| `web/src/panels/DES.tsx`, `DES.test.tsx` | `PanelSpec` olarak dışa aktarım; `usePanelData`'ya geçiş; testlerin bir kısmı `common.test.tsx`'e taşınır |
| `web/src/app/Shell.tsx` | komut kutusu → parser → navigate; şablon; uyarı; palet; `LAST_KEY` effect'i korunur |
| `web/src/app/CommandPalette.tsx` | cmdk paleti (düz `<Command>`): mnemonikler + sembol araması + önek notu |
| `web/src/app/keys.ts` | `useGlobalKeys` |
| `web/src/api/client.ts` | `searchSymbols`, `getFinancials`, `getDataset`, `getNews` + tipler |
| `web/src/app/styles.css` | tablo, sekme, liste, uyarı, palet (cmdk seçicileri) |
| `web/package.json`, `web/package-lock.json` | `cmdk` |

---

### Task 1: `/ui/api/symbols/{symbol}/news` rotası

**Files:**
- Create: `src/yfin/ui/data.py`
- Modify: `src/yfin/ui/__init__.py` (`install` + docstring)
- Test: `tests/unit/test_ui_data.py`, `tests/repo/test_ui_news_repo.py`

**Interfaces:**
- Produces: `GET /ui/api/symbols/{symbol}/news?limit=` → `Collection[NewsOut]`; `NewsOut { news_id, title, summary, pub_date, provider_name, link, thumbnail_url }`; `NEWS_DEFAULT_LIMIT = 50`, `NEWS_MAX_LIMIT = 200`; `list_news(session, symbol, limit) -> list[NewsOut]`.
- Consumes: `yfin.ui.router.UiSession`; `yfin.api.storage.session.session_scope` (`SessionDep` burada tanımlanır; `yfin.api.storage.session` onu dışa aktarmaz, `market.py` ve `datasets.py` kendi kopyalarını tanımlar); `yfin.api.storage.limits.apply_statement_timeout`; `yfin.api.schemas.common.Collection`; `yfin.core.normalize.normalize_symbol`; `yfin.models.news.News`, `NewsSymbol`.

- [ ] **Step 1: Birim testini yaz**

```python
# tests/unit/test_ui_data.py
"""UI-only data routes: cookie gate and parameter validation, no database."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yfin.api.core.config import ApiSettings
from yfin.ui import session
from yfin.ui.session import COOKIE_NAME

KEY = "k" * 32
PW = "hunter2"


def settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None, jwt_signing_key=KEY, jwt_kid="k1", jwt_issuer="yfin-api",
        ui_enabled=True, ui_password=PW,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from yfin.api.app import create_app
    from yfin.ui import pages

    monkeypatch.setattr(pages, "default_dist_dir", lambda: tmp_path / "absent")
    return TestClient(create_app(settings()))


def test_news_without_a_cookie_is_401(client: TestClient) -> None:
    response = client.get("/ui/api/symbols/AAPL/news")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthenticated"


def test_news_with_a_bearer_header_is_still_401(client: TestClient) -> None:
    response = client.get("/ui/api/symbols/AAPL/news", headers={"Authorization": "Bearer x"})
    assert response.status_code == 401


def test_news_limit_above_the_cap_is_422(client: TestClient) -> None:
    """The session dependency comes first in the signature, so with a valid
    cookie but a bad limit the handler refuses before touching a database."""
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    response = client.get("/ui/api/symbols/AAPL/news", params={"limit": 201})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_news_limit_zero_is_422(client: TestClient) -> None:
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    response = client.get("/ui/api/symbols/AAPL/news", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_the_route_is_not_in_the_schema(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/ui") for p in paths)
```

Not: `limit=201` testi handler'a girer ve `session_scope` bağımlılığı çözülür; `get_session_factory()` motoru tembel kurar, sorgu çalışmadığı için PostgreSQL'e bağlanılmaz (unit conftest `YF_SETTINGS_SOURCE=env` ile ayar tablosunu da okumaz). 422 kontrolü `apply_statement_timeout`'tan ÖNCE yapılır — aşağıdaki sırayı koru.

- [ ] **Step 2: Başarısızlığı doğrula**

Run: `uv run pytest tests/unit/test_ui_data.py -v`
Expected: 401 testleri catch-all yüzünden `404 != 401` ile FAIL; 422 testleri FAIL.

- [ ] **Step 3: Rotayı yaz**

```python
# src/yfin/ui/data.py
"""Read routes that exist only for the browser terminal.

The public `/v1` surface deliberately does not join: "news about AAPL" is
two calls there (`news_symbols` for the ids, then `news`). The page wants
one list, newest first, so the join lives here, behind the session cookie
and outside the OpenAPI document. Promoting it to `/v1` is a separate
decision (spec, "Kararlar" 8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from yfin.api.core.errors import TYPE_INVALID_PARAMETER, ApiProblem
from yfin.api.schemas.common import Collection
from yfin.api.storage import limits
from yfin.api.storage.session import session_scope
from yfin.core.normalize import normalize_symbol
from yfin.models.news import News, NewsSymbol
from yfin.ui.router import UiSession

router = APIRouter(prefix="/ui/api", include_in_schema=False)

#: Same shape market.py and datasets.py declare for themselves; the
#: storage module exports only the generator.
SessionDep = Annotated[Session, Depends(session_scope)]

NEWS_DEFAULT_LIMIT = 50
NEWS_MAX_LIMIT = 200


class NewsOut(BaseModel):
    news_id: str
    title: str
    summary: str | None
    pub_date: datetime
    provider_name: str | None
    #: canonical_url when the source gives one, else the click-through.
    link: str | None
    thumbnail_url: str | None


def list_news(session: Session, symbol: str, limit: int) -> list[NewsOut]:
    stmt = (
        select(News)
        .join(NewsSymbol, NewsSymbol.news_id == News.news_id)
        .where(NewsSymbol.symbol == symbol)
        .order_by(News.pub_date.desc(), News.news_id.desc())
        .limit(limit)
    )
    return [
        NewsOut(
            news_id=row.news_id,
            title=row.title,
            summary=row.summary,
            pub_date=row.pub_date,
            provider_name=row.provider_name,
            link=row.canonical_url or row.click_through_url,
            thumbnail_url=row.thumbnail_url,
        )
        for row in session.scalars(stmt)
    ]


@router.get("/symbols/{symbol}/news", response_model=Collection[NewsOut])
def symbol_news(
    _claims: UiSession,
    session: SessionDep,
    symbol: str,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> Collection[NewsOut]:
    size = limit if limit is not None else NEWS_DEFAULT_LIMIT
    if size > NEWS_MAX_LIMIT:
        raise ApiProblem(
            422, TYPE_INVALID_PARAMETER, "Page size above the maximum",
            detail=f"limit must not exceed {NEWS_MAX_LIMIT}",
        )
    limits.apply_statement_timeout(session)
    rows = list_news(session, normalize_symbol(symbol), size)
    # as_of stays None: the news table keeps no fetch timestamp on the row.
    return Collection[NewsOut](data=rows, next_cursor=None, as_of=None)
```

`src/yfin/ui/__init__.py` `install`: docstring "Order matters" cümlesini üç katmana göre yeniden yaz ve:

```python
    from yfin.ui import data, pages, router

    # Three layers, in this order: the data routes, then the API router
    # (which ends with a catch-all 404 for /ui/api/*), then the pages.
    # Starlette matches in registration order.
    app.include_router(data.router)
    app.include_router(router.router)
```

- [ ] **Step 4: Birim testleri geçir**

Run: `uv run pytest tests/unit/test_ui_data.py tests/unit/test_ui_router.py tests/unit/test_ui_pages.py -v`
Expected: PASS.

- [ ] **Step 5: Repo testini yaz ve koş**

`tests/repo/test_api_read_endpoints.py`'deki `seeded`/`client` kalıbını kopyala ve şu farkları uygula: `api_settings()` yardımcısı `ui_enabled=True, ui_password=PW` taşır (yoksa `create_app` UI'ı kurmaz ve rota 404 olur); `pages.default_dist_dir` `tmp_path/"absent"`'a monkeypatch'lenir; kimlik Bearer değil çerezdir: `token, _ = session.issue(api_settings()); client.cookies.set(COOKIE_NAME, token)`. `News` satırları için zorunlu sütunlar: `news_id`, `title`, `pub_date`, `raw_json` (NOT NULL; `"{}"` yeterli). `NewsSymbol.is_known` server default'ludur.

```python
# tests/repo/test_ui_news_repo.py (özet; fixture gövdeleri mevcut dosyadan)
pytestmark = pytest.mark.repo

def test_news_is_joined_per_symbol_newest_first(client, seeded) -> None:
    body = client.get("/ui/api/symbols/AAPL/news").json()
    ids = [row["news_id"] for row in body["data"]]
    assert ids == ["n-newer", "n-older"]          # MSFT's article absent
    assert body["data"][0]["link"] == "https://example.com/canonical"
    assert body["data"][1]["link"] == "https://example.com/click"  # no canonical → click-through
    assert body["as_of"] is None and body["next_cursor"] is None


def test_news_limit_caps_the_list(client, seeded) -> None:
    body = client.get("/ui/api/symbols/AAPL/news", params={"limit": 1}).json()
    assert [row["news_id"] for row in body["data"]] == ["n-newer"]
```

Run: `uv run pytest -q -m repo tests/repo/test_ui_news_repo.py` (compose `timescaledb` ayakta; `DB_HOST=localhost DB_PORT=5432`, `.env`'deki DB kimlik bilgileri). Expected: PASS.

- [ ] **Step 6: Lint, tip, kontrat, commit**

Run: `uv run ruff check . && uv run mypy && uv run pytest -q && uv run python scripts/dump_openapi.py --check`

```bash
git add src/yfin/ui/data.py src/yfin/ui/__init__.py tests/unit/test_ui_data.py tests/repo/test_ui_news_repo.py
git commit -m "feat(ui): /ui/api/symbols/{symbol}/news joins news_symbols to news

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 2: Komut dili çekirdeği (types, registry, parser) ve API istemcisi eklemeleri

**Files:**
- Create: `web/src/commands/types.ts`, `web/src/commands/registry.ts`, `web/src/commands/parser.ts`
- Modify: `web/src/api/client.ts`
- Test: `web/src/commands/parser.test.ts`, `web/src/api/client.test.ts` (ekleme)

**Interfaces (Produces):**

```ts
// types.ts
export type PanelArgs = Record<string, string>;
export interface PanelProps { symbol: string | null; args: PanelArgs }
export interface PanelSpec {
  code: string; title: string; needsSymbol: boolean; layout: "single" | "headed";
  parseArgs(tokens: string[]): PanelArgs;          // raw tokens; throws Error(message)
  component: ComponentType<PanelProps>;
}
export interface Command { symbol: string | null; code: string; args: PanelArgs }
// registry.ts
export function registerPanel(spec: PanelSpec): void;
export function getPanel(code: string): PanelSpec | undefined;
export function listPanels(): PanelSpec[];
export function isMnemonic(token: string): boolean;
export function clearRegistry(): void;
// parser.ts
export interface ParseContext { symbol: string | null; code: string }
export type ParseResult = { kind: "command"; command: Command } | { kind: "empty" } | { kind: "error"; message: string };
export function parse(input: string, ctx: ParseContext): ParseResult;
export const SYMBOL_RE: RegExp;
export function commandToPath(command: Command): string;                 // encodes the symbol
export function pathToCommand(symbol: string, code: string, search: string): Command; // symbol already decoded (useParams)
```

`client.ts` eklemeleri: `SymbolSummary`, `FinancialFact`, `NewsItem` tipleri; `searchSymbols(prefix)`, `getFinancials(symbol, statement, freq)`, `getDataset(name, symbol, params?)`, `getNews(symbol)`.

- [ ] **Step 1: Parser testlerini yaz**

```ts
// web/src/commands/parser.test.ts
import { beforeEach, describe, expect, it } from "vitest";
import { clearRegistry, registerPanel } from "./registry";
import { commandToPath, parse, pathToCommand } from "./parser";
import type { PanelArgs, PanelSpec } from "./types";

const Noop = () => null;

function joined(t: string[]): PanelArgs {
  const args: PanelArgs = {};
  if (t.length > 0) args.a = t.join(" ");
  return args;
}

function spec(
  code: string,
  needsSymbol = true,
  parseArgs: (t: string[]) => PanelArgs = joined,
): PanelSpec {
  return { code, title: code, needsSymbol, layout: "single", parseArgs, component: Noop };
}

const INTERVALS = ["1m", "5m", "15m", "60m"];

beforeEach(() => {
  clearRegistry();
  registerPanel(spec("DES"));
  registerPanel(spec("FA"));
  registerPanel(spec("CF"));
  registerPanel(spec("HELP", false, () => ({})));
  registerPanel(spec("GIP", true, (t) => {
    const interval = (t[0] ?? "5m").toLowerCase();
    if (!INTERVALS.includes(interval)) throw new Error(`Unknown interval ${t[0]}`);
    return { interval };
  }));
});

const ctx = { symbol: "AAPL", code: "DES" };

describe("parse", () => {
  it.each([
    ["", { kind: "empty" }],
    ["   ", { kind: "empty" }],
    ["msft", { kind: "command", command: { symbol: "MSFT", code: "DES", args: {} } }],
    ["FA", { kind: "command", command: { symbol: "AAPL", code: "FA", args: {} } }],
    ["msft fa", { kind: "command", command: { symbol: "MSFT", code: "FA", args: {} } }],
    ["CF DES", { kind: "command", command: { symbol: "CF", code: "DES", args: {} } }],
    ["cf", { kind: "command", command: { symbol: "AAPL", code: "CF", args: {} } }],
    ["GIP 15m", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "15m" } } }],
    ["GIP 15M", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "15m" } } }],
    ["AAPL GIP 1m", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "1m" } } }],
    ["HELP", { kind: "command", command: { symbol: null, code: "HELP", args: {} } }],
    ["BRK-B", { kind: "command", command: { symbol: "BRK-B", code: "DES", args: {} } }],
    ["^GSPC DES", { kind: "command", command: { symbol: "^GSPC", code: "DES", args: {} } }],
    ["FA DES x", { kind: "command", command: { symbol: "FA", code: "DES", args: { a: "x" } } }],
  ])("%j", (input, expected) => {
    expect(parse(input, ctx)).toEqual(expected);
  });

  it("keeps the panel when only a symbol is typed, unless the panel needs no symbol", () => {
    expect(parse("MSFT", { symbol: null, code: "HELP" })).toEqual({
      kind: "command", command: { symbol: "MSFT", code: "DES", args: {} },
    });
    expect(parse("MSFT", { symbol: "AAPL", code: "FA" })).toEqual({
      kind: "command", command: { symbol: "MSFT", code: "FA", args: {} },
    });
  });

  it("refuses a function that needs a symbol when there is none", () => {
    expect(parse("FA", { symbol: null, code: "HELP" })).toEqual({
      kind: "error", message: "FA needs a symbol: type one first, e.g. AAPL FA",
    });
  });

  it("rejects an invalid symbol token and an unknown function", () => {
    expect(parse("A$B", ctx)).toEqual({ kind: "error", message: "A$B is not a symbol" });
    expect(parse("AAPL XYZ", ctx)).toEqual({ kind: "error", message: "Unknown function XYZ" });
    expect(parse("XYZ", { symbol: null, code: "HELP" })).toEqual({
      kind: "command", command: { symbol: "XYZ", code: "DES", args: {} },
    });
  });

  it("surfaces parseArgs errors without a command, keeping the user's spelling", () => {
    expect(parse("GIP 3m", ctx)).toEqual({ kind: "error", message: "Unknown interval 3m" });
  });
});

describe("paths", () => {
  it("round-trips a command through the URL", () => {
    const cmd = { symbol: "AAPL", code: "GIP", args: { interval: "5m" } };
    expect(commandToPath(cmd)).toBe("/ui/t/AAPL/GIP?interval=5m");
    expect(pathToCommand("AAPL", "GIP", "?interval=5m")).toEqual(cmd);
  });
  it("uses '-' for no symbol and encodes odd symbols on the way out only", () => {
    expect(commandToPath({ symbol: null, code: "HELP", args: {} })).toBe("/ui/t/-/HELP");
    expect(commandToPath({ symbol: "^GSPC", code: "DES", args: {} })).toBe("/ui/t/%5EGSPC/DES");
    // useParams hands us the decoded segment, so no second decode here.
    expect(pathToCommand("-", "HELP", "")).toEqual({ symbol: null, code: "HELP", args: {} });
    expect(pathToCommand("^gspc", "des", "")).toEqual({ symbol: "^GSPC", code: "DES", args: {} });
  });
});
```

- [ ] **Step 2: Başarısızlığı doğrula**

Run: `cd web && npm test -- parser`
Expected: FAIL, modüller yok.

- [ ] **Step 3: Modülleri yaz**

```ts
// web/src/commands/types.ts
import type { ComponentType } from "react";

export type PanelArgs = Record<string, string>;

export interface PanelProps {
  symbol: string | null;
  args: PanelArgs;
}

export interface PanelSpec {
  code: string;
  title: string;
  needsSymbol: boolean;
  layout: "single" | "headed";
  /** Turns the raw tokens after the code into args. Throws Error(message) on bad input. */
  parseArgs(tokens: string[]): PanelArgs;
  component: ComponentType<PanelProps>;
}

export interface Command {
  symbol: string | null;
  code: string;
  args: PanelArgs;
}
```

```ts
// web/src/commands/registry.ts
import type { PanelSpec } from "./types";

const panels = new Map<string, PanelSpec>();

export function registerPanel(spec: PanelSpec): void {
  panels.set(spec.code.toUpperCase(), spec);
}

export function getPanel(code: string): PanelSpec | undefined {
  return panels.get(code.toUpperCase());
}

export function listPanels(): PanelSpec[] {
  return [...panels.values()].sort((a, b) => a.code.localeCompare(b.code));
}

export function isMnemonic(token: string): boolean {
  return panels.has(token.toUpperCase());
}

/** Tests register their own minimal panels. */
export function clearRegistry(): void {
  panels.clear();
}
```

```ts
// web/src/commands/parser.ts
// The command grammar: [SYMBOL] [CODE] [ARGS...]. Deterministic on purpose —
// no fuzzy matching, no guessing. Symbols and codes are compared upper-case;
// argument tokens reach parseArgs exactly as typed, because "15m" is an
// interval and "15M" is not. The one ambiguity (a ticker that is also a
// mnemonic, like CF) is settled by position: two leading mnemonics mean the
// first is the symbol.
import { getPanel, isMnemonic } from "./registry";
import type { Command, PanelArgs } from "./types";

export const SYMBOL_RE = /^[A-Z0-9.^=-]+$/;
const DEFAULT_CODE = "DES";

export interface ParseContext {
  symbol: string | null;
  code: string;
}

export type ParseResult =
  | { kind: "command"; command: Command }
  | { kind: "empty" }
  | { kind: "error"; message: string };

function build(symbol: string | null, code: string, rawArgs: string[]): ParseResult {
  const spec = getPanel(code);
  if (!spec) return { kind: "error", message: `Unknown function ${code}` };
  if (spec.needsSymbol && symbol === null) {
    return { kind: "error", message: `${code} needs a symbol: type one first, e.g. AAPL ${code}` };
  }
  let args: PanelArgs;
  try {
    args = spec.parseArgs(rawArgs);
  } catch (err) {
    return { kind: "error", message: err instanceof Error ? err.message : String(err) };
  }
  return { kind: "command", command: { symbol: spec.needsSymbol ? symbol : null, code: spec.code, args } };
}

export function parse(input: string, ctx: ParseContext): ParseResult {
  const raw = input.trim().split(/\s+/).filter(Boolean);
  if (raw.length === 0) return { kind: "empty" };
  const upper = raw.map((t) => t.toUpperCase());
  const first = upper[0] ?? "";
  const second = upper[1];

  if (raw.length === 1) {
    if (isMnemonic(first)) return build(ctx.symbol, first, []);
    if (!SYMBOL_RE.test(first)) return { kind: "error", message: `${first} is not a symbol` };
    // A bare symbol keeps the current panel, unless that panel takes no symbol.
    const current = getPanel(ctx.code);
    const code = current && current.needsSymbol ? current.code : DEFAULT_CODE;
    return build(first, code, []);
  }

  // Two or more tokens.
  if (second !== undefined && isMnemonic(first) && !isMnemonic(second)) {
    // "GIP 15m": function plus its arguments, on the context symbol.
    return build(ctx.symbol, first, raw.slice(1));
  }
  // "CF DES", "AAPL GIP 1m": symbol, function, arguments.
  if (!SYMBOL_RE.test(first)) return { kind: "error", message: `${first} is not a symbol` };
  if (second === undefined || !isMnemonic(second)) {
    return { kind: "error", message: `Unknown function ${second ?? ""}` };
  }
  return build(first, second, raw.slice(2));
}

export function commandToPath(command: Command): string {
  const symbol = command.symbol === null ? "-" : encodeURIComponent(command.symbol);
  const query = new URLSearchParams(command.args).toString();
  return `/ui/t/${symbol}/${command.code}${query ? `?${query}` : ""}`;
}

/** `symbol` and `code` are the path segments as react-router hands them over: already decoded. */
export function pathToCommand(symbol: string, code: string, search: string): Command {
  const args: PanelArgs = {};
  new URLSearchParams(search).forEach((value, key) => {
    args[key] = value;
  });
  return { symbol: symbol === "-" ? null : symbol.toUpperCase(), code: code.toUpperCase(), args };
}
```

`client.ts`'e ekle:

```ts
export interface SymbolSummary {
  symbol: string;
  short_name: string | null;
  long_name: string | null;
  exchange: string | null;
  quote_type: string | null;
}

export interface FinancialFact {
  period_end: string;
  item_key: string;
  value: string;
  currency: string | null;
}

export interface NewsItem {
  news_id: string;
  title: string;
  summary: string | null;
  pub_date: string;
  provider_name: string | null;
  link: string | null;
  thumbnail_url: string | null;
}

interface Page<T> {
  data: T[];
  next_cursor: string | null;
}

export const SEARCH_MIN_PREFIX = 2;

export async function searchSymbols(prefix: string): Promise<SymbolSummary[]> {
  const q = prefix.trim().toUpperCase();
  if (q.length < SEARCH_MIN_PREFIX) return [];
  const page = await apiFetch<Page<SymbolSummary>>(`/v1/symbols?q=${encodeURIComponent(q)}&limit=20`);
  return page.data;
}

const FINANCIALS_PAGE = 1000;
const FINANCIALS_MAX_PAGES = 5;

export async function getFinancials(symbol: string, statement: string, freq: string): Promise<FinancialFact[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const base = `/v1/symbols/${code}/financials?statement=${statement}&freq=${freq}&limit=${FINANCIALS_PAGE}`;
  const rows: FinancialFact[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < FINANCIALS_MAX_PAGES; i += 1) {
    const url: string = cursor ? `${base}&cursor=${encodeURIComponent(cursor)}` : base;
    const page: Page<FinancialFact> = await apiFetch<Page<FinancialFact>>(url);
    rows.push(...page.data);
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  return rows;
}

export async function getDataset(
  name: string, symbol: string, params: Record<string, string> = {},
): Promise<Record<string, unknown>[]> {
  const search = new URLSearchParams({ symbol: symbol.trim().toUpperCase(), limit: "200", ...params });
  const page = await apiFetch<Page<Record<string, unknown>>>(`/v1/datasets/${name}?${search}`);
  return page.data;
}

export async function getNews(symbol: string): Promise<NewsItem[]> {
  const code = encodeURIComponent(symbol.trim().toUpperCase());
  const page = await apiFetch<Page<NewsItem>>(`/ui/api/symbols/${code}/news`);
  return page.data;
}
```

`client.test.ts`'e üç test: `searchSymbols("a")` fetch çağırmadan `[]`; `getFinancials` `next_cursor` dolu ilk sayfadan sonra `cursor=` ile ikinci sayfayı ister ve satırları birleştirir; `getDataset("recommendations", "aapl", { filing_type: "10-K" })` URL'de `symbol=AAPL&limit=200&filing_type=10-K` taşır.

- [ ] **Step 4: Geçir, lint, commit**

Run: `cd web && npm test && npm run check && npm run lint`

```bash
git add web/src/commands web/src/api/client.ts web/src/api/client.test.ts
git commit -m "feat(web): command grammar, panel registry and REST client calls

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 3: Panel altyapısı, `DES`'in taşınması ve `HELP`

**Files:**
- Create: `web/src/panels/common.tsx`, `web/src/panels/common.test.tsx`, `web/src/panels/HELP.tsx`, `web/src/panels/HELP.test.tsx`, `web/src/panels/index.ts`
- Modify: `web/src/panels/DES.tsx`, `web/src/panels/DES.test.tsx`, `web/src/app/styles.css`

**Interfaces (Produces, `common.tsx`):**

```ts
export type Loaded<T> =
  | { kind: "loading" } | { kind: "ready"; data: T } | { kind: "empty" }
  | { kind: "missing" } | { kind: "error"; message: string };

/** Loads while the session is authenticated; re-runs when `key` changes or the session
 *  comes back after a login; drops stale responses; 401 → requireLogin (state stays
 *  "loading" until the session returns); 404 → missing; isEmpty(data) → empty. */
export function usePanelData<T>(
  key: string,
  load: () => Promise<T>,
  isEmpty?: (data: T) => boolean,
): { state: Loaded<T>; retry: () => void };

export function ErrorCard(props: { message: string; onRetry: () => void }): ReactElement;   // "Could not load: {message}" + Retry
export function EmptyCard(props: { what: string }): ReactElement;                          // "No {what} for this symbol."
export function MissingCard(props: { symbol: string | null }): ReactElement;               // "No such symbol: {symbol}"

export interface Column<Row> {
  key: string;
  label: string;
  align?: "left" | "right";
  format?: (row: Row) => ReactNode;      // default: String(row[key] ?? "—")
}
export function DataTable<Row extends Record<string, unknown>>(props: {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  selected?: number;
  onSelect?: (index: number) => void;
}): ReactElement;

/** j/k/Enter over `count` rows while no INPUT/TEXTAREA is focused. */
export function useListKeys(
  count: number,
  onEnter: (index: number) => void,
): [selected: number, setSelected: (index: number) => void];
```

- `panels/index.ts`: `export function registerAll(): void` — `registerPanel(DES_PANEL); registerPanel(HELP_PANEL);` (T5–T7 ekler). `main.tsx` ve `App.test.tsx` `registerAll()` çağırır (T4).
- `DES.tsx`: `export const DES_PANEL: PanelSpec = { code: "DES", title: "Description", needsSymbol: true, layout: "headed", parseArgs: () => ({}), component: DES }`; `DES({ symbol }: PanelProps)`; `symbol === null` ise `null` döner; veri `usePanelData(symbol, () => getSymbol(symbol))`; `missing` → `MissingCard`, `error` → `ErrorCard`; `INFO_ROWS`, `formatBig`, `asNumber` aynen kalır ve `formatBig` dışa aktarımı korunur (T5 kullanır).
- `DES.test.tsx`: `<DES symbol="AAPL" args={{}} />`; "waits for a session and loads once it is there" ve "ignores a stale response when the symbol changes mid-flight" testleri `common.test.tsx`'e taşınır (hook düzeyinde); DES'te kalanlar: identity/info satırları, `No such symbol`, Retry kartı, `formatBig`.
- `HELP.tsx`: `HELP_PANEL` (`needsSymbol: false`, `layout: "single"`); `listPanels()`'i `code — title (needs a symbol / no symbol)` satırlarıyla listeler; kısayolları (`Ctrl/⌘+K`, `/`, `Esc`, `Shift+Esc`, `?`, `j`, `k`, `Enter`) ve grammar'ı iki cümleyle anlatır.

- [ ] **Step 1: Testleri yaz**

`common.test.tsx` (RTL `renderHook` + `SessionProvider` sarmalayıcı, fetch mock'u `/ui/api/me` için):
- oturum `authenticated:false` iken `load` çağrılmaz; `refresh` ile `true` olunca bir kez çağrılır;
- `key` değişince ilk çağrının geç gelen yanıtı atılır (deferred promise);
- `load` `ApiError(404)` fırlatırsa `missing`; `[]` ve `isEmpty` ile `empty`; `TypeError` ile `error` ve `retry()` yeniden çağırır;
- `isEmpty` her render'da yeni lambda verildiğinde `load` yalnız bir kez çağrılır (sonsuz döngü regresyonu);
- `useListKeys`: `j` +1, `k` −1 (sınırlar `0..count-1`), `Enter` `onEnter(selected)`; bir `<input>` odaktayken tuşlar yok sayılır.

`HELP.test.tsx`: `clearRegistry()` + iki sahte panel (`needsSymbol` true/false); `HELP` her ikisini kod ve başlıkla listeler, `no symbol` etiketini doğru panele koyar.

`DES.test.tsx` güncellemesi yukarıdaki gibi.

- [ ] **Step 2: Uygula**

```tsx
// web/src/panels/common.tsx (çekirdek; kartlar ve DataTable JSX'i aşağıda tarif edildiği gibi)
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { ApiError, UnauthorizedError } from "../api/client";
import { useSession } from "../app/session";

export function usePanelData<T>(key: string, load: () => Promise<T>, isEmpty?: (data: T) => boolean) {
  const { me, requireLogin } = useSession();
  const authenticated = me?.authenticated === true;
  const [state, setState] = useState<Loaded<T>>({ kind: "loading" });
  // Refs, not deps: panels pass inline lambdas, and a new function each
  // render must not restart the load.
  const loadRef = useRef(load);
  loadRef.current = load;
  const isEmptyRef = useRef(isEmpty);
  isEmptyRef.current = isEmpty;
  const tokenRef = useRef({ cancelled: false });

  const run = useCallback(async () => {
    tokenRef.current.cancelled = true;
    const token = { cancelled: false };
    tokenRef.current = token;
    setState({ kind: "loading" });
    try {
      const data = await loadRef.current();
      if (token.cancelled) return;
      const empty = isEmptyRef.current;
      setState(empty && empty(data) ? { kind: "empty" } : { kind: "ready", data });
    } catch (err) {
      if (token.cancelled) return;
      if (err instanceof UnauthorizedError) requireLogin();   // stays "loading"; re-runs after login
      else if (err instanceof ApiError && err.status === 404) setState({ kind: "missing" });
      else setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }, [requireLogin]);

  useEffect(() => {
    if (!authenticated) return;
    void run();
    return () => {
      tokenRef.current.cancelled = true;
    };
  }, [key, authenticated, run]);

  const retry = useCallback(() => {
    if (authenticated) void run();
  }, [authenticated, run]);

  return { state, retry };
}
```

`useListKeys`: `useEffect` içinde `window.addEventListener("keydown", handler)`; handler önce `const el = document.activeElement; if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return;`; `j` → `Math.min(count - 1, s + 1)`, `k` → `Math.max(0, s - 1)`, `Enter` → `onEnter(selected)`; `count` değişince `selected` `0..count-1` içine kırpılır.

`DataTable`: `<table className="grid">`; başlıkta `column.label`, `align === "right"` için `.num`; satırda `column.format ? column.format(row) : String(row[column.key] ?? "—")`; `selected === index` için `className="row-selected"` ve `aria-selected="true"`; `onClick` → `onSelect?.(index)`.

`styles.css` ekleri (adlar sabit): `.grid`, `.grid th`, `.grid td.num`, `.row-selected`, `.tabs`, `.tab`, `.tab-active`, `.list`, `.list-row`, `.detail`, `.warn`, `.card`, `.card-error`, `.card-empty`.

- [ ] **Step 3: Geçir, lint, commit**

Run: `cd web && npm test && npm run check && npm run lint`

```bash
git add web/src/panels web/src/app/styles.css
git commit -m "feat(web): panel data hook, cards, table, list keys; DES as a PanelSpec; HELP

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 4: `Shell` yeniden yazımı: parser, gezinme, uyarı, kısayollar, cmdk paleti

**Files:**
- Modify: `web/src/app/Shell.tsx`, `web/src/app/App.tsx`, `web/src/app/main.tsx`, `web/src/app/App.test.tsx`, `web/src/app/styles.css`, `web/package.json`, `web/package-lock.json`
- Create: `web/src/app/CommandPalette.tsx`, `web/src/app/CommandPalette.test.tsx`, `web/src/app/keys.ts`

**Interfaces:**
- `Shell`: `useParams()` + `useLocation().search` → `pathToCommand`; `getPanel(code)`; şablon `spec.layout`; şerit (`.strip`, sembol varsa; 1c canlı hâle getirir); komut kutusu `aria-label="command"`, `autoFocus` korunur; `LAST_KEY` `useEffect`'i (1a'daki gibi her `(symbol, code)` değişiminde) **korunur**.
  - Enter → `submit(draft)`: `parse(draft, { symbol: command.symbol, code: command.code })`:
    - `empty` → hiçbir şey; `error` → `.warn` satırında mesaj, kutu temizlenmez;
    - `command` → sembol değiştiyse `getSymbol` ile doğrula: 404 → uyarı "No such symbol X", `pending = { code, args }` saklanır, palet `query = X` ile açılır; `UnauthorizedError` → `requireLogin()` ve `pendingDraft = draft` (login sonrası `authenticated` false→true olunca `submit(pendingDraft)` bir kez); diğer hata → "Could not reach the API. Try again."; OK → `setWarning(null)`, `setDraft("")`, `navigate(commandToPath(cmd))`.
  - Palet seçimi `onPick(symbol)`: `pending` varsa `navigate(commandToPath({ symbol, code: pending.code, args: pending.args }))` (doğrulama gerekmez, palet API'den geldi), yoksa `submit(symbol)`; palet kapanır, `pending` silinir.
  - `!spec` → "Unknown function {code}."; `spec.needsSymbol && !command.symbol` → "Type a symbol to begin."; aksi hâlde `<spec.component symbol={command.symbol} args={command.args} />`.
- `keys.ts` `useGlobalKeys({ inputRef, paletteOpen, openHelp })`: `keydown` dinleyicisi; palet açıkken çık; `Esc`: odak komut kutusundaysa `setDraft("")` + `blur()`, değilse `navigate(-1)`; `Shift+Esc` → `navigate(1)`; diğer kısayollar (`Ctrl/Meta+K`, `/`, `?`) yalnız odak bir input'ta değilken: `k` → `focus()`, `/` → `focus()` (+`preventDefault`), `?` → `openHelp()`.
- `CommandPalette` (`cmdk`, düz `<Command>`; `Command.Dialog` YOK): props `{ open, query, onQuery, onPick, onClose }`; overlay `div.palette-backdrop` + `div.palette`; `Command.Input` (`aria-label="palette"`, açılınca `autoFocus`); öğeler: kayıtlı mnemonikler `CODE — title` (`listPanels()`), ve `query.length >= SEARCH_MIN_PREFIX` ise `searchSymbols(query)` sonuçları `SYMBOL — long_name ?? short_name` (debounce 150 ms, stale sonuç atılır); listenin altında sabit not: "Symbol search matches the start of the ticker only, not company names."; seçim → `onPick(text)` (mnemonik seçildiyse `submit(code)`, sembolse yukarıdaki kural); `Esc` → `onClose()`.

- [ ] **Step 1: Testleri yaz**

`App.test.tsx` kurulumu:

```tsx
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { clearRegistry, registerPanel } from "../commands/registry";
import { registerAll } from "../panels";

beforeEach(() => {
  clearRegistry();
  registerAll();
  registerPanel({ code: "FAKEFA", title: "Fake", needsSymbol: true, layout: "single", parseArgs: () => ({}), component: () => <p>fake FA panel</p> });
  registerPanel({ code: "GIP", title: "Intraday", needsSymbol: true, layout: "headed",
    parseArgs: (t) => { const i = (t[0] ?? "5m").toLowerCase(); if (!["1m","5m","15m","60m"].includes(i)) throw new Error(`Unknown interval ${t[0]}`); return { interval: i }; },
    component: () => null });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });
```

Mevcut 1a testleri (login modalı, login → DES, `msft{enter}` → DES, `/ui` yönlendirmesi) **değişmeden** geçmeli. Yeni testler:
- (a) `msft fakefa{enter}` → `/v1/symbols/MSFT` doğrulanır, "fake FA panel" görünür, `screen.getByLabelText("command")` boş;
- (b) `NOPE{enter}` → `/v1/symbols/NOPE` 404 → "No such symbol NOPE" uyarısı, URL değişmez (DES paneli AAPL'da kalır), palet açılır (`getByLabelText("palette")`), `/v1/symbols?q=NOPE&limit=20` çağrılır; sonuçtaki `NOPES` öğesine tıklayınca `/v1/symbols/NOPES` çizilir (pending code = DES);
- (c) `gip 3m{enter}` → uyarı "Unknown interval 3m", URL değişmez;
- (d) Esc: `AAPL DES` → `MSFT DES` gezinmesinden sonra önce `Esc` (kutu odaklı → kutu boşalır ve odak gider), sonra ikinci `Esc` → `await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeInTheDocument())`;
- (e) `?` (kutu odaklı değilken) → HELP paneli (`registerAll` ile gerçek HELP) görünür;
- (f) `/` tuşu odak kutuda değilken kutuya odak verir.

`CommandPalette.test.tsx`: yazınca mnemonikler filtrelenir; `"MS"` yazınca `searchSymbols` çağrılır ve `MSFT — Microsoft` öğesine tıklayınca `onPick("MSFT")`; önek notu görünür; `Esc` `onClose`.

- [ ] **Step 2: `cmdk` ekle**

Run: `cd web && npm install --package-lock-only cmdk@1.1.1 && npm ci && git status --short`
Expected: yalnız `web/package.json` ve `web/package-lock.json` değişir (`cmdk` `dependencies`'te `^1.1.1`).

- [ ] **Step 3: Uygula**

`Shell.tsx` çekirdeği:

```tsx
const { symbol: rawSymbol = "-", code: rawCode = "DES" } = useParams();
const { search } = useLocation();
const navigate = useNavigate();
const { me, requireLogin } = useSession();
const command = useMemo(() => pathToCommand(rawSymbol, rawCode, search), [rawSymbol, rawCode, search]);
const spec = getPanel(command.code);
const inputRef = useRef<HTMLInputElement>(null);
const [draft, setDraft] = useState("");
const [warning, setWarning] = useState<string | null>(null);
const [palette, setPalette] = useState<{ open: boolean; query: string }>({ open: false, query: "" });
const pendingRef = useRef<{ code: string; args: PanelArgs } | null>(null);
const pendingDraftRef = useRef<string | null>(null);

useEffect(() => {
  try { localStorage.setItem(LAST_KEY, `/ui/t/${rawSymbol}/${rawCode}`); } catch { /* storage blocked */ }
}, [rawSymbol, rawCode]);

const submit = useCallback(async (text: string) => {
  const result = parse(text, { symbol: command.symbol, code: command.code });
  if (result.kind === "empty") return;
  if (result.kind === "error") { setWarning(result.message); return; }
  const next = result.command;
  if (next.symbol && next.symbol !== command.symbol) {
    try {
      await getSymbol(next.symbol);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setWarning(`No such symbol ${next.symbol}`);
        pendingRef.current = { code: next.code, args: next.args };
        setPalette({ open: true, query: next.symbol });
        return;
      }
      if (err instanceof UnauthorizedError) { pendingDraftRef.current = text; requireLogin(); return; }
      setWarning("Could not reach the API. Try again.");
      return;
    }
  }
  setWarning(null);
  setDraft("");
  void navigate(commandToPath(next));
}, [command, navigate, requireLogin]);

// The spec's "the last command re-runs after a successful login".
useEffect(() => {
  if (me?.authenticated && pendingDraftRef.current !== null) {
    const text = pendingDraftRef.current;
    pendingDraftRef.current = null;
    void submit(text);
  }
}, [me?.authenticated, submit]);

function onPick(text: string) {
  const pending = pendingRef.current;
  pendingRef.current = null;
  setPalette({ open: false, query: "" });
  if (pending && SYMBOL_RE.test(text.toUpperCase()) && !isMnemonic(text)) {
    setWarning(null);
    setDraft("");
    void navigate(commandToPath({ symbol: text.toUpperCase(), code: pending.code, args: pending.args }));
    return;
  }
  void submit(text);
}
```

`main.tsx`: `registerAll()` render'dan önce. `App.tsx` değişmez (rotalar aynı).

`styles.css`: `.palette-backdrop`, `.palette`, `[cmdk-input]`, `[cmdk-list]`, `[cmdk-item]`, `[cmdk-item][data-selected="true"]`, `[cmdk-group-heading]`, `.palette-note`.

- [ ] **Step 4: Geçir, lint, build, commit**

Run: `cd web && npm test && npm run check && npm run lint && npm run build`

```bash
git add web/package.json web/package-lock.json web/src/app
git commit -m "feat(web): command box drives the parser, history navigation, warnings and the cmdk palette

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 5: `FA` paneli

**Files:**
- Create: `web/src/panels/FA.tsx`, `web/src/panels/FA.test.tsx`
- Modify: `web/src/panels/index.ts`, `web/src/app/styles.css`

**Interfaces:**
- `FA_PANEL`: `code "FA"`, `title "Financial statements"`, `needsSymbol true`, `layout "single"`.
- `parseArgs(tokens)`: `const s = (tokens[0] ?? "income").toLowerCase()`, `const f = (tokens[1] ?? "annual").toLowerCase()`; `STATEMENTS = { income: "income", balance: "balance_sheet", cash: "cash_flow" }`, `FREQS = ["annual", "quarterly", "ttm"]`; bilinmeyen → `throw new Error("Usage: FA [income|balance|cash] [annual|quarterly|ttm]")`; döner `{ statement: STATEMENTS[s], freq: f }`.
- Bileşen: `args.statement` / `args.freq` (URL'den eksikse varsayılanlar); sekmeler statement ×3 ve freq ×3, tıklama `navigate(commandToPath({ symbol, code: "FA", args: {...} }))`; veri `usePanelData(\`${symbol}|${statement}|${freq}\`, () => getFinancials(symbol!, statement, freq), (rows) => rows.length === 0)`.
- Pivot: `periods = uniq(rows.map(r => r.period_end)).sort().reverse().slice(0, 8)`; `items = uniq(rows.map(r => r.item_key))` ilk görülme sırasında; `cell = new Map(\`${item_key}|${period_end}\` → value)`; tablo satırı `{ item: item_key, ...periods → cell }`; `DataTable` sütunları: `item` (sol) + her period (sağ, `format: formatBig(asNumber(value))`, yoksa `"—"`); `formatBig` ve `asNumber` `./DES`'ten import edilir (`asNumber` dışa aktarılır); `currency` (ilk satırdan) panel başlığında.

- [ ] **Step 1: Test** — mock `/v1/symbols/AAPL/financials?statement=income&freq=annual&limit=1000` üç satır iki dönem (`TotalRevenue` `391035000000` ve `NetIncome`), `next_cursor: null`; tablo başlıklarında iki tarih, satırda `TotalRevenue` ve `391.04B`; sekme `balance` tıklanınca URL `?statement=balance_sheet&freq=annual` (MemoryRouter + `useLocation` probe bileşeni) ve yeni istek; boş `data` → "No financial statements for this symbol."; `parseArgs` tablosu (`["balance","quarterly"]` → `{statement:"balance_sheet",freq:"quarterly"}`, `["BALANCE"]` → aynı statement + annual, `["x"]` → usage hatası).
- [ ] **Step 2: Uygula** (yukarıdaki pivot; `registerAll`'a `FA_PANEL`).
- [ ] **Step 3: Geçir, lint, commit**

```bash
git add web/src/panels/FA.tsx web/src/panels/FA.test.tsx web/src/panels/index.ts web/src/panels/DES.tsx web/src/app/styles.css
git commit -m "feat(web): FA panel with statement and frequency tabs

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 6: `ANR` paneli

**Files:**
- Create: `web/src/panels/ANR.tsx`, `web/src/panels/ANR.test.tsx`
- Modify: `web/src/panels/index.ts`

**Interfaces:**
- `ANR_PANEL`: `code "ANR"`, `title "Analyst ratings"`, `needsSymbol true`, `layout "single"`, `parseArgs: () => ({})`.
- Tek `load`: beş `getDataset` çağrısı `Promise.allSettled` ile; sonuç `Sections = { targets, recommendations, grades, estimates, trend }`, her biri `{ rows } | { error: string }`. **401 kuralı:** `allSettled` sonuçlarından herhangi biri `UnauthorizedError` ile reddedildiyse `load` onu yeniden fırlatır (böylece `usePanelData` `requireLogin()` çağırır). Hepsi boşsa `isEmpty` true → `EmptyCard("analyst data")`.
- Bölümler ve sütunlar:
  - `analyst_price_targets` (ilk satır): `current`, `low`, `mean`, `median`, `high` — tek satır özet.
  - `recommendations`: `as_of_date`, `period`, `strong_buy`, `buy`, `hold`, `sell`, `strong_sell` — ilk 4.
  - `upgrades_downgrades`: `grade_ts_utc`, `firm`, `from_grade → to_grade`, `action` — ilk 20.
  - `earnings_estimate`: `period`, `avg`, `low`, `high`, `number_of_analysts`, `growth` — en yeni `as_of_date`'in satırları.
  - `eps_trend`: `period`, `current`, `days_ago_7`, `days_ago_30`, `days_ago_60`, `days_ago_90` — en yeni `as_of_date`.
  - Hatalı bölüm → o bölümde `ErrorCard` (`onRetry` → panelin `retry`); boş bölüm → "No … " satırı.

- [ ] **Step 1: Test** — beş URL için mock (biri `[]`, biri 500): dört tablo başlığı görünür, boş olan "No recommendations", 500 olan Retry kartı; bir istek 401 verirse `requireLogin` etkisi (`/ui/api/me` probe ile `session:false`).
- [ ] **Step 2: Uygula** (`registerAll`'a `ANR_PANEL`).
- [ ] **Step 3: Geçir, lint, commit**

```bash
git add web/src/panels/ANR.tsx web/src/panels/ANR.test.tsx web/src/panels/index.ts
git commit -m "feat(web): ANR panel over five analyst datasets

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 7: `N` ve `CF` panelleri

**Files:**
- Create: `web/src/panels/N.tsx`, `web/src/panels/N.test.tsx`, `web/src/panels/CF.tsx`, `web/src/panels/CF.test.tsx`
- Modify: `web/src/panels/index.ts`, `web/src/app/styles.css`

**Interfaces:**
- `N_PANEL`: `code "N"`, `title "News"`, `single`, `parseArgs: () => ({})`. `usePanelData(symbol, () => getNews(symbol!), r => r.length === 0)`; liste kendi `<ul className="list">` bileşenidir (`DataTable` değil): satır = `new Date(pub_date).toLocaleString()`, `provider_name ?? "—"`, `title`; `useListKeys(rows.length, setOpen)`; seçili satır `.row-selected`; `Enter` veya tıklama `open` indeksini set eder ve altta `.detail` alanında `summary` (yoksa "No summary.") ve `link` (`<a href target="_blank" rel="noopener noreferrer">Open article</a>`) gösterilir. Boş → `EmptyCard("news")`.
- `CF_PANEL`: `code "CF"`, `title "SEC filings"`, `single`, `parseArgs(tokens)`: `tokens[0]` varsa `{ filing_type: tokens[0].toUpperCase() }` yoksa `{}`. `load`: `Promise.all([getDataset("sec_filings", symbol, args.filing_type ? { filing_type } : {}), getDataset("sec_filing_exhibits", symbol)])`; ekler istemcide `filing_id`'ye göre `Map`'lenir. Liste kendi bileşeni: satır = `filing_date`, `filing_type`, `title ?? "—"`, `exhibit_count`, `edgar_url` linki; `useListKeys` ile `Enter`/tıklama satırı genişletir (`expanded` indeksi; tekrar Enter kapatır) ve altına `exhibit_type` + `url` listesi; ek yoksa "No exhibits fetched for this filing (the exhibits feed is capped at 200 rows)." Boş → `EmptyCard("filings")`.

- [ ] **Step 1: Testler** — N: iki makale, `j` sonra `Enter` ikinci makalenin özetini açar, link `href` doğru; boş liste kartı. CF: iki dosya + biri için iki ek; `CF 10-k` → `parseArgs` `{ filing_type: "10-K" }`, istek URL'inde `filing_type=10-K`; `Enter` ile ilk dosya genişleyince iki ek görünür, ikinci dosyada "No exhibits fetched" satırı.
- [ ] **Step 2: Uygula** (`registerAll`'a ikisi de).
- [ ] **Step 3: Geçir, lint, build, commit**

```bash
git add web/src/panels/N.tsx web/src/panels/N.test.tsx web/src/panels/CF.tsx web/src/panels/CF.test.tsx web/src/panels/index.ts web/src/app/styles.css
git commit -m "feat(web): N and CF panels

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 8: Uçtan uca doğrulama, spec ve README güncellemesi

**Files:**
- Modify: `README.md` ("Web terminal" bölümüne komut örnekleri), `docs/superpowers/specs/2026-09-07-web-terminal-design.md`

- [ ] **Step 1: Tam doğrulama** — Global Constraints'teki Python ve web komutlarının tamamı.
- [ ] **Step 2: Elle kontrol** — compose DB ayakta: `YFAPI_UI_ENABLED=true YFAPI_UI_PASSWORD=<seç> YFAPI_JWT_SIGNING_KEY=<32+ karakter> YFAPI_REDIS_URL=redis://localhost:6381/0 DB_HOST=localhost DB_PORT=5432 uv run uvicorn yfin.api.app:app --port 8001`; tarayıcıda: `AAPL FA`, `FA balance quarterly`, `ANR`, `N` (+ `j`, `Enter`), `CF 10-K`, `HELP`, `NOPE` (uyarı + palet), `Esc` ×2 geri. Konsolda CSP ihlali yok. Gözlemi commit gövdesine yaz; ortam yoksa "koşulmadı".
- [ ] **Step 3: Spec güncellemesi** — "Komut dili ve panel modeli": grammar paragrafını bu planın Global Constraints'teki üç dallı kuralla değiştir ve "Token'lar büyük harfe çevrilir" cümlesini "Sembol ve fonksiyon token'ları büyük harfle karşılaştırılır; argüman token'ları `parseArgs`'a olduğu gibi verilir" yap; araştırma tablosunda `react-hotkeys-hook` ve `@tanstack/react-table` satırlarını "1b'de kullanılmadı: tek `keydown` dinleyicisi / kendi `DataTable`" notuyla güncelle; "Hata yönetimi"nde "TanStack Query 3 deneme" yerine "otomatik yeniden deneme yok; hata kartında Retry"; bu planın Global Constraints'teki "1a'da Query kullanılmaz" notunu "1b'de de `usePanelData`" olarak spec'e taşı.
- [ ] **Step 4: README** — "Web terminal" altına:

```bash
AAPL            # description (DES) of a symbol
FA balance qtr  # not valid: FA takes income|balance|cash and annual|quarterly|ttm
FA balance quarterly
ANR             # analyst ratings on the current symbol
N               # news; j/k to move, Enter to open
CF 10-K         # SEC filings of one type; Enter expands exhibits
HELP            # every function and shortcut; Esc goes back
```

- [ ] **Step 5: Commit** — `docs(ui): command examples, spec aligned with the 1b grammar`.

---

## Self-review notları

- Spec kapsamı (1b): parser/registry (T2), gezinme, uyarı, kısayollar, cmdk (T4), `HELP` (T3), `FA` (T5), `ANR` (T6), `N` + `/ui/api/.../news` (T1, T7), `CF` (T7); spec sapmaları T8'de yazılır.
- Tip tutarlılığı: `PanelProps.args` zorunlu, `DES.test.tsx` `args={{}}` verir; `useListKeys` dönüşü `[selected, setSelected]` etiketli tuple; `DataTable<Row>` generic, `rowKey` zorunlu; `Column.format(row)` tek argüman; `pathToCommand` decode etmez; `parseArgs` ham token alır ve kendi `toLowerCase`/`toUpperCase`'ini yapar; `SessionDep` `data.py` içinde tanımlı.
- İnceleme sonrası düzeltmeler (2026-09-07): argümanlar ham; `noUncheckedIndexedAccess` uyumlu parser; test yardımcısı tipli; `SessionDep` yerel; `DES.test.tsx` planda; `registerAll` + `clearRegistry` `App.test.tsx`'te; Esc testi iki basış; cmdk `Dialog` yasak ve `--package-lock-only`; `pendingCode` ile palet düzeltme akışı; `pendingDraft` ile 401 sonrası yeniden gönderim; `usePanelData` `isEmptyRef` ve `retry` oturum kontrolü; ANR 401 yeniden fırlatma; CF `filing_type`; N/CF kendi liste bileşenleri; `formatBig`/`asNumber` DES'ten; repo testi `ui_enabled` + `raw_json`; `install` docstring; `.warn`/palet stil seçicileri; `LAST_KEY` effect'i korunur.
- Bilinen sınırlar: URL'den gelen args `parseArgs` ile doğrulanmaz (elle yazılmış `?statement=bogus` → API 422 → hata kartı); `sec_filing_exhibits` 200 satırla sınırlı; `news` sayfalama yok (son 50).
