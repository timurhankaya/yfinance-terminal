// What a saved page IS, and how it travels.
//
// A layout does not fit in a URL. `WLA`'s symbol list did -- it is a
// comma-separated string -- but a grid of four panels, their sizes and
// their arguments is not something a reader can be asked to carry in an
// address bar. So a page is a JSON document: the letters it points at,
// and dockview's own serialisation of everything else.
//
// **There is no `panels` field beside `dock`.** `api.toJSON()` already
// carries each panel's `params`, which is what it is showing; a second
// map of the same thing would disagree with it the first time someone
// dragged a panel. `dock` is the single source, and `groups` is the only
// page-level fact there is.
//
// The document is portable on purpose. Phase 2b wants to keep pages in
// the database for a signed-in reader; a `Page` can be written there
// unchanged, which is the guarantee that this storage decision does not
// become a migration.
import type { SerializedDockview } from "dockview-react";
import type { PanelParams, PanelSeed } from "../app/Workspace";
import { getPanel } from "../commands/registry";
import type { PanelArgs } from "../commands/types";
import { GROUP_ORDER, Group } from "./groups";
import type { GroupSymbols } from "./groups";

/** The page with no name: what a reader is on before they save one. */
export enum PageName {
  Scratch = "-",
}

export interface Page {
  /** `PageName.Scratch` for the unnamed working page. */
  name: string;
  /** What each letter is pointed at. */
  groups: GroupSymbols;
  dock: SerializedDockview;
}

export interface PageStore {
  v: typeof STORE_VERSION;
  /** The F-key order. A page's position here is its key. */
  order: string[];
  pages: Record<string, Page>;
}

//: One version field, in the envelope. No migration code is written for
//: it: a lost layout is a preference that can be rebuilt in a minute,
//: not data that cannot be recovered (spec, "Kararlar" 9).
export const STORE_VERSION = 1;

/** Characters a shared link may carry.
 *
 *  A URL is safe end to end -- browser, proxy header buffer, access log
 *  -- to about 8 KB. Base64 inflates by a third, so 4,000 characters of
 *  layout is a ~5.3 KB address with room left over. */
export const SHARE_MAX = 4000;

export function emptyStore(): PageStore {
  return { v: STORE_VERSION, order: [], pages: {} };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Whether a decoded value is a page.
 *
 *  Shallow on purpose: `dock` is dockview's own document and only
 *  dockview can say whether it will load. A page that passes this and
 *  then fails `fromJSON` is dropped at that point, which is the second
 *  of the two granularities. */
export function isPage(value: unknown): value is Page {
  if (!isRecord(value)) return false;
  return typeof value.name === "string" && isRecord(value.groups) && isRecord(value.dock);
}

/** Whether what came out of storage is a store at all.
 *
 *  A `v` that does not match is not a store this build can read, and
 *  there is no migration path by decision -- so it is treated as
 *  corruption, which resets it. */
export function isPageStore(value: unknown): value is PageStore {
  if (!isRecord(value)) return false;
  if (value.v !== STORE_VERSION) return false;
  if (!Array.isArray(value.order) || !value.order.every((n) => typeof n === "string")) return false;
  return isRecord(value.pages) && Object.values(value.pages).every(isPage);
}

function asGroup(value: unknown): Group | null {
  return GROUP_ORDER.find((group) => group === value) ?? null;
}

function asArgs(value: unknown): PanelArgs {
  if (!isRecord(value)) return {};
  const args: PanelArgs = {};
  for (const [key, raw] of Object.entries(value)) {
    if (typeof raw === "string") args[key] = raw;
  }
  return args;
}

/** The panels a saved layout holds, as the shell's own seeds.
 *
 *  Every panel's args go back through `normalizeArgs`, for the same
 *  reason a URL's do: a stored page is an input the panel's `parseArgs`
 *  never saw, and it may have been hand-edited, shared by someone on an
 *  older build, or written before the panel changed its mind about what
 *  it accepts.
 *
 *  Symbols are NOT verified here. A page of seven panels would be seven
 *  extra requests before anything drew, and a symbol the archive has
 *  dropped shows its own missing-symbol card one panel later. */
export function seedsFromDock(dock: SerializedDockview): PanelSeed[] {
  const panels = isRecord(dock.panels) ? dock.panels : {};
  const seeds: PanelSeed[] = [];
  for (const id of Object.keys(panels).sort()) {
    const state: unknown = panels[id];
    if (!isRecord(state)) continue;
    const params = isRecord(state.params) ? state.params : {};
    const code = typeof params.code === "string" ? params.code : null;
    if (code === null) continue;
    const args = asArgs(params.args);
    const normalize = getPanel(code)?.normalizeArgs;
    seeds.push({
      id,
      code,
      symbol: typeof params.symbol === "string" ? params.symbol : null,
      args: normalize === undefined ? args : normalize(args),
      group: asGroup(params.group),
    });
  }
  return seeds;
}

/** The params dockview stores for one seed. */
export function paramsOf(seed: PanelSeed): PanelParams {
  return { code: seed.code, symbol: seed.symbol, args: seed.args, group: seed.group };
}

//: base64url, so the link survives a query string without escaping. The
//: text is UTF-8 first: a page name is whatever the reader typed.
function toBase64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(text: string): string {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/** A page as a link parameter, or null when it is too big to be one.
 *
 *  Null rather than a truncated link: half a layout would open as a page
 *  that is silently not the one that was shared. */
export function encodePage(page: Page): string | null {
  const encoded = toBase64Url(JSON.stringify(page));
  return encoded.length > SHARE_MAX ? null : encoded;
}

/** A page from a link parameter, or null when it is not one.
 *
 *  Anything can arrive here -- a truncated paste, a link from another
 *  build -- so every failure is the same answer and the working page is
 *  left alone. */
export function decodePage(encoded: string): Page | null {
  try {
    const parsed: unknown = JSON.parse(fromBase64Url(encoded));
    return isPage(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
