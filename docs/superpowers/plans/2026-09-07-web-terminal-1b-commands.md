# Web Terminal 1b: Komut Dili ve REST Panelleri — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Komut kutusuna `AAPL FA`, `ANR`, `N`, `CF`, `HELP` yazılabilen, sembolü doğrulayan, geçmişte Esc ile gezen ve dört REST panelini gösteren terminal.

**Architecture:** Komut dili saf TypeScript modüllerinde yaşar (`web/src/commands/`: parser, registry, `PanelSpec`); `Shell` bunları kullanarak URL üçlüsüne (`/ui/t/{SYMBOL}/{CODE}?args`) gider ve react-router history'si gezinme yığınıdır. Paneller `PanelSpec.component` olarak kaydedilir; `Shell` kayıtlı panelin şablonuna (`single` / `headed`) göre çizer. Backend'e yalnızca bir UI rotası eklenir: `/ui/api/symbols/{s}/news` (`news_symbols ⋈ news`), çünkü generic dataset yüzeyi join yapmaz. Diğer paneller mevcut `/v1` rotalarını çerezle çağırır.

**Tech Stack:** React 19, react-router 7, TypeScript, vitest + RTL, `cmdk` (yeni bağımlılık); Python 3.13 / FastAPI / SQLAlchemy 2.

**Spec:** `docs/superpowers/specs/2026-09-07-web-terminal-design.md` — bölümler "Komut dili ve panel modeli", "Hata yönetimi", "Uygulama sırası" (1b satırı), "Testler".

## Global Constraints

- Committed `openapi.json` **değişmez**: yeni rota `/ui/api` altında ve `include_in_schema=False`. Her task sonunda `uv run python scripts/dump_openapi.py --check`.
- Python: `uv run ruff check . && uv run mypy && uv run pytest -q` yeşil. Bilinen taban: `src/yfin/outbox/kafka.py`'de 3 eski mypy hatası; yeni hata olmaz.
- Web: `cd web && npm run check && npm run lint && npm test && npm run build` yeşil, çıktı temiz (`act()` uyarısı yok). vitest `globals: false`: her test dosyası `afterEach` içinde RTL `cleanup()` çağırır. `npm install` çalıştırılmaz; bağımlılık eklerken `npm install --legacy-peer-deps <pkg>` sonra `npm ci` ile lockfile doğrulanır.
- Inline `style=` yok (CSP `style-src 'self'`); tüm stil `styles.css`. UI metinleri ve yorumlar İngilizce.
- Grammar (spec): `[SYMBOL] [CODE] [ARGS...]`; token'lar büyük harf; sembol `/^[A-Z0-9.^=-]+$/`; tek token mnemonik → fonksiyon; tek token değil → sembol, panel korunur (sembolsüz panel açıksa `DES`); iki+ token ve ilk ikisi de mnemonik → ilki sembol; `parseArgs` hatası → uyarı, yığına itilmez.
- URL: `/ui/t/{SYMBOL}/{CODE}?{args}`; sembolsüz `/ui/t/-/HELP`. Esc = `history.back()`, Shift+Esc = `forward()`; komut kutusu odaktayken Esc önce kutuyu kapatır. Ctrl+K / Meta+K / `/` kutuya odak; `?` HELP; `j`/`k`/`Enter` liste panellerinde; kısayollar bir input odaktayken pasif.
- Sembol doğrulama: `/v1/symbols/{s}`; 404 → kutunun altında uyarı ve palet `/v1/symbols?q=` (önek, en az 2 karakter) sonuçlarıyla; bağlam değişmez.
- `/ui/api/symbols/{s}/news`: yalnız çerez (`UiSession`); `limit` varsayılan 50, en çok 200; `pub_date desc, news_id desc`; yanıt `{"data":[...], "as_of": null}`.
- 1a'nın UI istekleri ölçülmez kuralı geçerli; `page_size_cap` UI için 1000 (financials `limit=1000` kullanılabilir).
- Commit mesajı `Claude-Session:` satırıyla biter; e-posta adresi içeren trailer kullanılmaz.
- Test sabitleri: `KEY = "k" * 32`, `PW = "hunter2"`; `ApiSettings(_env_file=None, jwt_kid="k1", jwt_issuer="yfin-api", ...)`.

---

## Dosya yapısı

| Dosya | Sorumluluk |
| --- | --- |
| `src/yfin/ui/data.py` | UI'a özel okuma rotaları: `/ui/api/symbols/{symbol}/news`; router `install`'da `router.router`'dan ÖNCE eklenir (catch-all'dan önce) |
| `src/yfin/ui/__init__.py` | `install`: `data.router` sonra `router.router` |
| `tests/unit/test_ui_data.py`, `tests/repo/test_ui_news_repo.py` | 401/422 birim; join ve sıralama repo |
| `web/src/commands/types.ts` | `PanelArgs`, `PanelSpec`, `Command` |
| `web/src/commands/parser.ts` | `parse(input, registry, context) -> ParseResult` |
| `web/src/commands/registry.ts` | `registerPanel`, `getPanel`, `listPanels`, kayıtlı mnemonikler |
| `web/src/panels/index.ts` | tüm panelleri registry'ye kaydeder (`DES`, `HELP`, `FA`, `ANR`, `N`, `CF`) |
| `web/src/panels/{HELP,FA,ANR,N,CF}.tsx` + testler | paneller |
| `web/src/panels/common.tsx` | `PanelState` hook (`usePanelData`), `ErrorCard`, `EmptyCard`, `DataTable`, `useListKeys` |
| `web/src/app/Shell.tsx` | komut kutusu → parser → navigate; şablon seçimi; uyarı satırı; palet |
| `web/src/app/CommandPalette.tsx` | cmdk paleti: mnemonikler + sembol araması |
| `web/src/app/keys.ts` | global kısayollar (`useGlobalKeys`) |
| `web/src/api/client.ts` | `searchSymbols`, `getFinancials`, `getDataset`, `getNews` |
| `web/src/app/styles.css` | tablo, sekme, liste, uyarı, palet stilleri |

---

### Task 1: `/ui/api/symbols/{symbol}/news` rotası

**Files:**
- Create: `src/yfin/ui/data.py`
- Modify: `src/yfin/ui/__init__.py` (`install`)
- Test: `tests/unit/test_ui_data.py`, `tests/repo/test_ui_news_repo.py`

**Interfaces:**
- Produces: `GET /ui/api/symbols/{symbol}/news?limit=` → `Collection[NewsOut]`; `NewsOut { news_id, title, summary, pub_date, provider_name, link, thumbnail_url }`; `NEWS_DEFAULT_LIMIT = 50`, `NEWS_MAX_LIMIT = 200`.
- Consumes: `yfin.ui.router.UiSession`; `yfin.api.storage.session.session_scope`; `yfin.api.storage.limits.apply_statement_timeout`; `yfin.api.schemas.common.Collection`; `yfin.core.normalize.normalize_symbol`; `yfin.models.news.News`, `NewsSymbol`.

- [ ] **Step 1: Birim testini yaz**

```python
# tests/unit/test_ui_data.py
"""UI-only data routes: cookie gate and parameter validation, no database."""

from __future__ import annotations

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
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
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
    token, _ = session.issue(settings())
    client.cookies.set(COOKIE_NAME, token)
    response = client.get("/ui/api/symbols/AAPL/news", params={"limit": 201})
    assert response.status_code == 422
    assert response.json()["type"] == "invalid_parameter"


def test_the_route_is_not_in_the_schema(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not any(p.startswith("/ui") for p in paths)
```

- [ ] **Step 2: Başarısızlığı doğrula**

Run: `uv run pytest tests/unit/test_ui_data.py -v`
Expected: 401 testleri catch-all yüzünden 404 döndüğü için FAIL (`assert 404 == 401`); 422 testi FAIL.

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

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from yfin.api.core.errors import TYPE_INVALID_PARAMETER, ApiProblem
from yfin.api.schemas.common import Collection
from yfin.api.storage import limits
from yfin.api.storage.session import SessionDep
from yfin.core.normalize import normalize_symbol
from yfin.models.news import News, NewsSymbol
from yfin.ui.router import UiSession

router = APIRouter(prefix="/ui/api", include_in_schema=False)

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

`SessionDep` `yfin.api.storage.session`'da yoksa (`market.py` kendi tanımlıyorsa) aynı iki satırı burada tanımla: `SessionDep = Annotated[Session, Depends(session_scope)]`.

`src/yfin/ui/__init__.py` `install` içinde:

```python
    from yfin.ui import data, pages, router

    # data first: router.router ends with a catch-all 404 for /ui/api/*,
    # and Starlette matches in registration order.
    app.include_router(data.router)
    app.include_router(router.router)
```

- [ ] **Step 4: Birim testleri geçir**

Run: `uv run pytest tests/unit/test_ui_data.py tests/unit/test_ui_router.py tests/unit/test_ui_pages.py -v`
Expected: PASS. `_claims` parametresi ilk sırada olduğu için çerez yokken `session_scope` hiç çalışmaz ve DB'ye dokunulmaz.

- [ ] **Step 5: Repo testini yaz ve koş**

`tests/repo/test_api_read_endpoints.py`'deki `seeded`/`client` kalıbını izle: `News` ve `NewsSymbol` satırları ekle (iki makale AAPL'a, biri MSFT'ye; AAPL'ınkilerden biri daha yeni), `app.dependency_overrides[api_session.session_scope]` ile test şemasına bağla, çerezi `session.issue(api_settings())` ile kur.

```python
# tests/repo/test_ui_news_repo.py (özet; fixture'ları mevcut dosyadan kopyala)
def test_news_is_joined_per_symbol_newest_first(client, seeded) -> None:
    body = client.get("/ui/api/symbols/AAPL/news").json()
    ids = [row["news_id"] for row in body["data"]]
    assert ids == ["n-newer", "n-older"]          # MSFT's article absent
    assert body["data"][0]["link"] == "https://example.com/canonical"
    assert body["as_of"] is None


def test_news_limit_caps_the_list(client, seeded) -> None:
    body = client.get("/ui/api/symbols/AAPL/news", params={"limit": 1}).json()
    assert len(body["data"]) == 1
```

Run: `uv run pytest -q -m repo tests/repo/test_ui_news_repo.py` (PostgreSQL gerekir; compose `timescaledb` ayakta, `DB_HOST=localhost`). Expected: PASS.

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

**Interfaces:**
- Produces:

```ts
// types.ts
export type PanelArgs = Record<string, string>;
export interface PanelProps { symbol: string | null; args: PanelArgs }
export interface PanelSpec {
  code: string;                      // "FA"
  title: string;                     // "Financial statements"
  needsSymbol: boolean;
  layout: "single" | "headed";
  parseArgs(tokens: string[]): PanelArgs;   // throws Error(message) on invalid
  component: React.ComponentType<PanelProps>;
}
export interface Command { symbol: string | null; code: string; args: PanelArgs }
// registry.ts
export function registerPanel(spec: PanelSpec): void;
export function getPanel(code: string): PanelSpec | undefined;
export function listPanels(): PanelSpec[];          // sorted by code
export function isMnemonic(token: string): boolean;
export function clearRegistry(): void;               // tests only
// parser.ts
export interface ParseContext { symbol: string | null; code: string }
export type ParseResult =
  | { kind: "command"; command: Command }
  | { kind: "empty" }
  | { kind: "error"; message: string };
export function parse(input: string, ctx: ParseContext): ParseResult;
export const SYMBOL_RE = /^[A-Z0-9.^=-]+$/;
export function commandToPath(command: Command): string;   // "/ui/t/AAPL/FA?freq=annual"
export function pathToCommand(symbol: string, code: string, search: string): Command;
```

  - `client.ts`: `searchSymbols(prefix: string): Promise<SymbolSummary[]>` (`/v1/symbols?q=&limit=20`, `data`), `getFinancials(symbol, statement, freq): Promise<FinancialFact[]>` (limit 1000, `next_cursor` varsa en fazla 5 sayfa daha), `getDataset(name, symbol, params?): Promise<Record<string, unknown>[]>` (`/v1/datasets/{name}?symbol=&limit=200`), `getNews(symbol): Promise<NewsItem[]>` (`/ui/api/symbols/{s}/news`). Tipler: `SymbolSummary { symbol, short_name, long_name, exchange, quote_type }`, `FinancialFact { period_end: string; item_key: string; value: string; currency: string | null }`, `NewsItem { news_id, title, summary, pub_date, provider_name, link, thumbnail_url }`.

- [ ] **Step 1: Parser testlerini yaz (tablo odaklı)**

```ts
// web/src/commands/parser.test.ts
import { beforeEach, describe, expect, it } from "vitest";
import { clearRegistry, registerPanel } from "./registry";
import { commandToPath, parse, pathToCommand } from "./parser";

const Noop = () => null;
function spec(code: string, needsSymbol = true, parseArgs = (t: string[]) => (t.length ? { a: t.join(" ") } : {})) {
  return { code, title: code, needsSymbol, layout: "single" as const, parseArgs, component: Noop };
}

beforeEach(() => {
  clearRegistry();
  registerPanel(spec("DES"));
  registerPanel(spec("FA"));
  registerPanel(spec("CF"));
  registerPanel(spec("HELP", false, () => ({})));
  registerPanel(spec("GIP", true, (t) => {
    const interval = t[0] ?? "5m";
    if (!["1m", "5m", "15m", "60m"].includes(interval)) throw new Error(`Unknown interval ${interval}`);
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
    ["AAPL GIP 1m", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "1m" } } }],
    ["HELP", { kind: "command", command: { symbol: null, code: "HELP", args: {} } }],
    ["BRK-B", { kind: "command", command: { symbol: "BRK-B", code: "DES", args: {} } }],
    ["^GSPC DES", { kind: "command", command: { symbol: "^GSPC", code: "DES", args: {} } }],
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

  it("rejects an invalid symbol token", () => {
    expect(parse("AA PL@", ctx)).toEqual({ kind: "error", message: "Unknown function PL@" });
    expect(parse("A$B", ctx)).toEqual({ kind: "error", message: "A$B is not a symbol" });
  });

  it("surfaces parseArgs errors without a command", () => {
    expect(parse("GIP 3m", ctx)).toEqual({ kind: "error", message: "Unknown interval 3m" });
  });

  it("does not treat a three-token input's first token as a function", () => {
    expect(parse("FA DES x", ctx)).toEqual({ kind: "command", command: { symbol: "FA", code: "DES", args: { a: "x" } } });
  });
});

describe("paths", () => {
  it("round-trips a command through the URL", () => {
    const cmd = { symbol: "AAPL", code: "GIP", args: { interval: "5m" } };
    expect(commandToPath(cmd)).toBe("/ui/t/AAPL/GIP?interval=5m");
    expect(pathToCommand("AAPL", "GIP", "?interval=5m")).toEqual(cmd);
  });
  it("uses '-' for no symbol and encodes odd symbols", () => {
    expect(commandToPath({ symbol: null, code: "HELP", args: {} })).toBe("/ui/t/-/HELP");
    expect(commandToPath({ symbol: "^GSPC", code: "DES", args: {} })).toBe("/ui/t/%5EGSPC/DES");
    expect(pathToCommand("-", "HELP", "")).toEqual({ symbol: null, code: "HELP", args: {} });
    expect(pathToCommand("%5EGSPC", "DES", "")).toEqual({ symbol: "^GSPC", code: "DES", args: {} });
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
  /** Turns the tokens after the code into args. Throws Error(message) on bad input. */
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
// no fuzzy matching, no guessing. The one ambiguity (a ticker that is also
// a mnemonic, like CF) is settled by position: two leading mnemonics mean
// the first is the symbol.
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

function build(symbol: string | null, code: string, rest: string[]): ParseResult {
  const spec = getPanel(code);
  if (!spec) return { kind: "error", message: `Unknown function ${code}` };
  if (spec.needsSymbol && symbol === null) {
    return { kind: "error", message: `${code} needs a symbol: type one first, e.g. AAPL ${code}` };
  }
  let args: PanelArgs;
  try {
    args = spec.parseArgs(rest);
  } catch (err) {
    return { kind: "error", message: err instanceof Error ? err.message : String(err) };
  }
  return { kind: "command", command: { symbol: spec.needsSymbol ? symbol : null, code: spec.code, args } };
}

export function parse(input: string, ctx: ParseContext): ParseResult {
  const tokens = input.trim().split(/\s+/).filter(Boolean).map((t) => t.toUpperCase());
  if (tokens.length === 0) return { kind: "empty" };
  const [first, second, ...rest] = tokens;

  if (tokens.length === 1) {
    if (isMnemonic(first)) return build(ctx.symbol, first, []);
    if (!SYMBOL_RE.test(first)) return { kind: "error", message: `${first} is not a symbol` };
    // A bare symbol keeps the current panel, unless that panel takes no symbol.
    const current = getPanel(ctx.code);
    const code = current && current.needsSymbol ? current.code : DEFAULT_CODE;
    return build(first, code, []);
  }

  // Two or more tokens. "CF DES" is CF Industries' DES; "FA DES x" likewise.
  if (isMnemonic(first) && !isMnemonic(second ?? "")) {
    return build(ctx.symbol, first, [second!, ...rest]);
  }
  if (!SYMBOL_RE.test(first)) return { kind: "error", message: `${first} is not a symbol` };
  if (!isMnemonic(second ?? "")) return { kind: "error", message: `Unknown function ${second}` };
  return build(first, second!, rest);
}

export function commandToPath(command: Command): string {
  const symbol = command.symbol === null ? "-" : encodeURIComponent(command.symbol);
  const query = new URLSearchParams(command.args).toString();
  return `/ui/t/${symbol}/${command.code}${query ? `?${query}` : ""}`;
}

export function pathToCommand(symbol: string, code: string, search: string): Command {
  const decoded = decodeURIComponent(symbol);
  const args: PanelArgs = {};
  new URLSearchParams(search).forEach((value, key) => {
    args[key] = value;
  });
  return { symbol: decoded === "-" ? null : decoded.toUpperCase(), code: code.toUpperCase(), args };
}
```

Not: `parse("FA", ...)` iki token dalındaki `isMnemonic(first) && !isMnemonic(second)` kuralı "GIP 15m" için ilk token fonksiyon, ikinci arg; "CF DES" için ilki sembol. Testte `"FA DES x"` → sembol FA. Beklenen davranış spec'in kuralıdır.

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

export async function searchSymbols(prefix: string): Promise<SymbolSummary[]> {
  const q = prefix.trim().toUpperCase();
  if (q.length < 2) return [];
  const page = await apiFetch<Page<SymbolSummary>>(
    `/v1/symbols?q=${encodeURIComponent(q)}&limit=20`,
  );
  return page.data;
}

const FINANCIALS_PAGE = 1000;
const FINANCIALS_MAX_PAGES = 5;

export async function getFinancials(
  symbol: string, statement: string, freq: string,
): Promise<FinancialFact[]> {
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

`client.test.ts`'e üç test ekle: `searchSymbols("a")` fetch çağırmadan `[]` döner; `getFinancials` `next_cursor` dolu ilk sayfadan sonra ikinci sayfayı ister ve satırları birleştirir; `getDataset("recommendations", "aapl")` URL'de `symbol=AAPL&limit=200` taşır.

- [ ] **Step 4: Geçir, lint, commit**

Run: `cd web && npm test && npm run check && npm run lint`

```bash
git add web/src/commands web/src/api/client.ts web/src/api/client.test.ts
git commit -m "feat(web): command grammar, panel registry and REST client calls

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 3: Panel altyapısı ve `HELP`

**Files:**
- Create: `web/src/panels/common.tsx`, `web/src/panels/HELP.tsx`, `web/src/panels/index.ts`
- Modify: `web/src/panels/DES.tsx` (`PanelSpec` olarak dışa aktar; `symbol: string | null` prop'u), `web/src/app/styles.css`
- Test: `web/src/panels/common.test.tsx`, `web/src/panels/HELP.test.tsx`

**Interfaces:**
- Produces (`common.tsx`):

```ts
export type Loaded<T> =
  | { kind: "loading" } | { kind: "ready"; data: T } | { kind: "empty" }
  | { kind: "missing" } | { kind: "error"; message: string };
/** Loads once per (key) while authenticated; re-runs after login; cancels stale responses;
 *  401 → requireLogin; 404 → missing; [] → empty (isEmpty predicate). */
export function usePanelData<T>(key: string, load: () => Promise<T>, isEmpty?: (d: T) => boolean): { state: Loaded<T>; retry(): void };
export function ErrorCard({ message, onRetry }): JSX.Element;      // "Could not load. <Retry>"
export function EmptyCard({ what }): JSX.Element;                  // "No {what} for this symbol."
export function DataTable({ columns, rows, selected, onSelect }): JSX.Element; // columns: {key,label,align?,format?}
/** j/k/Enter over `count` rows while no input is focused. */
export function useListKeys(count: number, onEnter: (index: number) => void): [selected: number, setSelected];
```

  - `panels/index.ts`: `registerAll()` — `DES`, `HELP` (bu task), sonraki task'larda `FA`, `ANR`, `N`, `CF` eklenir. `main.tsx` ve testler `registerAll()` çağırır.
  - `DES.tsx`: `export const DES_PANEL: PanelSpec = { code: "DES", title: "Description", needsSymbol: true, layout: "headed", parseArgs: () => ({}), component: DES }`; `DES` artık `usePanelData` kullanır (mevcut davranış ve testler korunur; `tokenRef` mantığı `usePanelData`'ya taşınır).
  - `HELP.tsx`: `HELP_PANEL` (`needsSymbol: false`, `layout: "single"`); `listPanels()`'i kod/başlık/sembol-gerektirir olarak listeler ve kısayolları yazar.

- [ ] **Step 1: Testler**

`common.test.tsx`: `usePanelData` — (a) oturum yokken yüklemez, login sonrası yükler; (b) key değişince eski yanıt atılır (Task 8/1a'daki senaryonun hook düzeyinde tekrarı); (c) 404 → `missing`, boş dizi → `empty`, TypeError → `error` ve Retry yeniden dener; `useListKeys` — `j`/`k` seçimi sınırlar içinde taşır, `Enter` `onEnter(selected)` çağırır, input odaktayken hiçbir şey yapmaz.
`HELP.test.tsx`: iki panel kayıtlıyken kodları ve "needs symbol" işaretini listeler.

- [ ] **Step 2: Uygula**

`usePanelData` iskeleti:

```tsx
export function usePanelData<T>(key: string, load: () => Promise<T>, isEmpty?: (d: T) => boolean) {
  const { me, requireLogin } = useSession();
  const authenticated = me?.authenticated === true;
  const [state, setState] = useState<Loaded<T>>({ kind: "loading" });
  const tokenRef = useRef({ cancelled: false });
  const loadRef = useRef(load);
  loadRef.current = load;

  const run = useCallback(async () => {
    tokenRef.current.cancelled = true;
    const token = { cancelled: false };
    tokenRef.current = token;
    setState({ kind: "loading" });
    try {
      const data = await loadRef.current();
      if (token.cancelled) return;
      setState(isEmpty && isEmpty(data) ? { kind: "empty" } : { kind: "ready", data });
    } catch (err) {
      if (token.cancelled) return;
      if (err instanceof UnauthorizedError) requireLogin();
      else if (err instanceof ApiError && err.status === 404) setState({ kind: "missing" });
      else setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }, [requireLogin, isEmpty]);

  useEffect(() => {
    if (!authenticated) return;
    void run();
    return () => { tokenRef.current.cancelled = true; };
  }, [key, authenticated, run]);

  return { state, retry: () => void run() };
}
```

`useListKeys`: `window.addEventListener("keydown")`; `document.activeElement` bir `INPUT`/`TEXTAREA` ise çık; `j` → +1, `k` → −1 (0..count−1), `Enter` → `onEnter(selected)`.

`DataTable`: `<table className="grid">`, `columns[].format?(value, row)`; `selected` satıra `aria-selected` ve `.row-selected` sınıfı; `onSelect` tıklamada.

`styles.css` ekleri: `.grid` (sabit başlık, sağa hizalı sayı sütunları `.num`), `.row-selected`, `.tabs`/`.tab-active`, `.list`, `.warn` (komut kutusu altı uyarı), `.palette` (cmdk kapsayıcısı).

- [ ] **Step 3: Geçir, lint, commit**

Run: `cd web && npm test && npm run check && npm run lint`

```bash
git add web/src/panels web/src/app/styles.css
git commit -m "feat(web): panel data hook, table, list keys and the HELP panel

Claude-Session: https://claude.ai/code/session_01P8FqetHBXRA1B6GC1M6EmT"
```

---

### Task 4: `Shell` yeniden yazımı: parser, gezinme, uyarı, kısayollar, cmdk paleti

**Files:**
- Modify: `web/src/app/Shell.tsx`, `web/src/app/App.tsx`, `web/src/app/main.tsx`, `web/src/app/styles.css`, `web/package.json` (+`cmdk`)
- Create: `web/src/app/CommandPalette.tsx`, `web/src/app/keys.ts`
- Test: `web/src/app/App.test.tsx` (genişletme), `web/src/app/CommandPalette.test.tsx`

**Interfaces:**
- `Shell`: `useParams` + `useLocation().search` → `pathToCommand`; `getPanel(code)`; şablon `spec.layout`; sembol varsa şerit (`.strip`, 1c canlı hâle getirir); komut kutusu Enter → `parse(input, {symbol, code})`:
  - `empty` → hiçbir şey; `error` → `.warn` satırında mesaj, kutu temizlenmez;
  - `command` → sembol değiştiyse `getSymbol` ile doğrula: 404 → uyarı "No such symbol X" ve palet `searchSymbols(X)` sonuçlarıyla açılır; OK → `navigate(commandToPath(cmd))`, kutu temizlenir; `localStorage[LAST_KEY]` güncellenir.
  - Bilinmeyen kod URL'den gelirse "Unknown function X" kartı.
- `keys.ts` `useGlobalKeys({ focusBox, openHelp })`: `Ctrl/Meta+K`, `/` → odak (input odakta değilken); `?` → `navigate("/ui/t/-/HELP")`; `Esc` → odak kutudaysa blur+temizle, değilse `navigate(-1)`; `Shift+Esc` → `navigate(1)`.
- `CommandPalette` (`cmdk`): açık/kapalı prop'u; öğeler: kayıtlı mnemonikler (`code — title`) ve `query` ≥2 karakter ise `searchSymbols` sonuçları (`SYMBOL — long_name`); seçim → `onPick(text)` (Shell bunu `parse`'a verir). Kapatma: Esc.

- [ ] **Step 1: Testler**

`App.test.tsx`'e ekle (mock fetch, `cleanup()`): (a) `msft fa` → URL `/ui/t/MSFT/FA` ve FA paneli çizilir (FA henüz kayıtlı değilse testte `registerPanel` ile sahte FA kaydet); (b) `NOPE` → `/v1/symbols/NOPE` 404 → uyarı "No such symbol NOPE" görünür, URL değişmez, palet açılır ve `/v1/symbols?q=NOPE` çağrılır; (c) `Esc` `history.back()` (MemoryRouter'da iki girişten sonra Esc ilk sayfaya döner); (d) `?` HELP açar; (e) `GIP 3m` uyarısı (GIP testte sahte kayıt).
`CommandPalette.test.tsx`: yazınca mnemonik filtrelenir; 2+ karakterde `searchSymbols` çağrılır ve sonuç tıklanınca `onPick("MSFT")`.

- [ ] **Step 2: `cmdk` ekle**

Run: `cd web && npm install --legacy-peer-deps cmdk@^1.1.1 && npm ci && git status --short` (yalnız `package.json` ve `package-lock.json` değişmeli).

- [ ] **Step 3: Uygula**

`Shell.tsx` çekirdeği:

```tsx
const { symbol: rawSymbol = "-", code: rawCode = "DES" } = useParams();
const { search } = useLocation();
const command = useMemo(() => pathToCommand(rawSymbol, rawCode, search), [rawSymbol, rawCode, search]);
const spec = getPanel(command.code);
const [draft, setDraft] = useState("");
const [warning, setWarning] = useState<string | null>(null);
const [palette, setPalette] = useState<{ open: boolean; query: string }>({ open: false, query: "" });

async function submit(text: string) {
  const result = parse(text, { symbol: command.symbol, code: command.code });
  if (result.kind === "empty") return;
  if (result.kind === "error") { setWarning(result.message); return; }
  const next = result.command;
  if (next.symbol && next.symbol !== command.symbol) {
    try { await getSymbol(next.symbol); }
    catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setWarning(`No such symbol ${next.symbol}`);
        setPalette({ open: true, query: next.symbol });
        return;
      }
      if (err instanceof UnauthorizedError) { requireLogin(); return; }
      setWarning("Could not reach the API. Try again.");
      return;
    }
  }
  setWarning(null);
  setDraft("");
  void navigate(commandToPath(next));
}
```

Şablon: `spec.layout === "headed"` ise şerit; `spec.needsSymbol && !command.symbol` ise "Type a symbol to begin."; `!spec` ise "Unknown function {code}."; aksi hâlde `<spec.component symbol={command.symbol} args={command.args} />`.

`main.tsx`: `registerAll()` render'dan önce.

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
- `FA_PANEL`: `code "FA"`, `title "Financial statements"`, `needsSymbol true`, `layout "single"`, `parseArgs(tokens)`: `tokens[0]` ∈ `{income, balance, cash}` → `statement` (`income` → `income`, `balance` → `balance_sheet`, `cash` → `cash_flow`), `tokens[1]` ∈ `{annual, quarterly, ttm}` → `freq`; varsayılan `income`/`annual`; bilinmeyen → `Error("Usage: FA [income|balance|cash] [annual|quarterly|ttm]")`.
- Bileşen: sekmeler (statement ×3, freq ×3) URL args'ı `navigate` ile günceller (`commandToPath`); veri `getFinancials(symbol, statement, freq)`; pivot: satır = `item_key` (ilk görüldüğü sırada), sütun = `period_end` (yeniden eskiye, en fazla 8 dönem); değerler `formatBig`-benzeri (`asNumber` + K/M/B/T, negatifler parantezsiz `-`); `currency` başlıkta.

- [ ] **Step 1: Test** — mock `/v1/symbols/AAPL/financials?statement=income&freq=annual&limit=1000` üç satır iki dönem; tablo başlıklarında iki tarih, satırda `TotalRevenue` ve `391.04B`; sekme tıklayınca URL `?statement=balance_sheet&freq=annual` (MemoryRouter + `useLocation` probe); boş `data` → "No financial statements for this symbol."; `parseArgs` tablosu.
- [ ] **Step 2: Uygula** (`usePanelData(\`${symbol}|${statement}|${freq}\`, ...)`, `DataTable`).
- [ ] **Step 3: Geçir, commit** — `feat(web): FA panel with statement and frequency tabs`.

---

### Task 6: `ANR` paneli

**Files:**
- Create: `web/src/panels/ANR.tsx`, `web/src/panels/ANR.test.tsx`
- Modify: `web/src/panels/index.ts`

**Interfaces:**
- `ANR_PANEL`: `code "ANR"`, `title "Analyst ratings"`, `needsSymbol true`, `layout "single"`, `parseArgs: () => ({})`.
- Bileşen beş bölüm, her biri `getDataset(name, symbol)` ile ayrı yüklenir (`Promise.allSettled`; biri hata verirse o bölüm hata kartı, diğerleri çizilir):
  - `analyst_price_targets` (ilk satır): `current`, `low`, `mean`, `median`, `high` — özet satırı.
  - `recommendations`: `as_of_date`, `period`, `strong_buy`, `buy`, `hold`, `sell`, `strong_sell` — en yeni 4 satır.
  - `upgrades_downgrades`: `grade_ts_utc`, `firm`, `from_grade`→`to_grade`, `action` — en yeni 20.
  - `earnings_estimate`: `period`, `avg`, `low`, `high`, `number_of_analysts`, `growth` — en yeni `as_of_date`'in satırları.
  - `eps_trend`: `period`, `current`, `days_ago_7`, `days_ago_30`, `days_ago_60`, `days_ago_90`.
- Tümü boşsa `EmptyCard("analyst data")`.

- [ ] **Step 1: Test** — beş URL için mock (biri boş, biri 500): dört tablo görünür, boş olan "No … " yazar, 500 olan Retry kartı; başlık satırları doğru.
- [ ] **Step 2: Uygula** — `usePanelData` beş bölüm için tek `load` (`allSettled` sonucunu bir nesne olarak döndürür), bölüm başına `DataTable`.
- [ ] **Step 3: Geçir, commit** — `feat(web): ANR panel over five analyst datasets`.

---

### Task 7: `N` ve `CF` panelleri

**Files:**
- Create: `web/src/panels/N.tsx`, `web/src/panels/N.test.tsx`, `web/src/panels/CF.tsx`, `web/src/panels/CF.test.tsx`
- Modify: `web/src/panels/index.ts`, `web/src/app/styles.css`

**Interfaces:**
- `N_PANEL`: `code "N"`, `title "News"`, `single`, `parseArgs: () => ({})`. Liste `getNews(symbol)`; satır: tarih (yerel, `toLocaleString`), `provider_name`, `title`; `useListKeys` ile `j`/`k`, `Enter` seçili makalenin `summary`'sini ve `link`'ini (`<a target="_blank" rel="noopener noreferrer">`) alt panelde açar; tıklama da seçer. Boş → `EmptyCard("news")`.
- `CF_PANEL`: `code "CF"`, `title "SEC filings"`, `single`, `parseArgs(tokens)`: `tokens[0]` varsa `{ type: tokens[0] }` → `getDataset("sec_filings", symbol, { filing_type })`. Liste: `filing_date`, `filing_type`, `title`, `edgar_url` bağlantısı, `exhibit_count`; `Enter`/tıklama satırı genişletir ve `getDataset("sec_filing_exhibits", symbol, { filing_id })`… `filing_id` `filters`'da yok, bu yüzden ekler tek seferde `getDataset("sec_filing_exhibits", symbol)` (200 satır) ile alınır ve istemcide `filing_id`'ye göre gruplanır; genişletilen satırın altında `exhibit_type` + `url` listesi.

- [ ] **Step 1: Testler** — N: iki makale, `j` sonra `Enter` ikinci makalenin özetini açar, link `href` doğru; boş liste kartı. CF: iki dosya, `CF 10-K` → URL'de `filing_type=10-K` ve istek `filing_type=10-K`; satır genişletince ekler görünür.
- [ ] **Step 2: Uygula.**
- [ ] **Step 3: Geçir, lint, build, commit** — `feat(web): N and CF panels`.

---

### Task 8: Uçtan uca doğrulama ve belgeler

**Files:**
- Modify: `README.md` ("Web terminal" bölümüne komut örnekleri), `docs/superpowers/specs/2026-09-07-web-terminal-design.md` (yalnız 1b'de sapma olduysa)

- [ ] **Step 1: Tam doğrulama** — Python ve web komutlarının tamamı (Global Constraints).
- [ ] **Step 2: Elle kontrol** — compose DB ayakta: `YFAPI_UI_ENABLED=true YFAPI_UI_PASSWORD=<seç> YFAPI_REDIS_URL=redis://localhost:6381/0 DB_HOST=localhost uv run uvicorn yfin.api.app:app --port 8001`; tarayıcıda: `AAPL FA`, `FA balance quarterly`, `ANR`, `N` (+ `j`, `Enter`), `CF 10-K`, `HELP`, `NOPE` (uyarı + palet), `Esc` geri. Konsolda CSP ihlali yok. Gözlemi commit gövdesine yaz.
- [ ] **Step 3: README** — "Web terminal" altına 6 satırlık komut örneği bloğu.
- [ ] **Step 4: Commit** — `docs(ui): command examples for the web terminal`.

---

## Self-review notları

- Spec kapsamı (1b): parser/registry (T2), cmdk ve history gezinme, uyarı, kısayollar (T4), `HELP` (T3), `FA` (T5), `ANR` (T6), `N` + `/ui/api/.../news` (T1, T7), `CF` (T7). Şerit 1c'ye kadar sembol adını gösterir.
- Tip tutarlılığı: `PanelProps.symbol: string | null` — `DES` bileşeni `symbol` null olamaz varsayımıyla yazılmıştı; T3 `DES`'i `PanelProps`'a uyarlar (`if (!symbol) return null` yeterli, `Shell` sembolsüz durumda zaten çizmez).
- Bilinen sınırlar: `sec_filing_exhibits` `filing_id` ile filtrelenemediği için ekler 200 satırla çekilir (çok dosyalı sembollerde eksik kalabilir; 1.5'te `/ui/api` rotası ya da `filters` genişletmesi). `news` sayfalama yok (son 50).
