// Where saved pages live: `localStorage`, and nowhere else. A layout is
// the one piece of state that does not fit in an address. The blast
// radius is one module: moving pages to the database only changes what
// is behind these functions.
import { emptyStore, isPageStore } from "./page";
import type { Page, PageStore } from "./page";

export const STORE_KEY = "yfin.ui.pages";

/** The keys a page can be reached by, in order. F5, F11 and F12 are
 *  missing because a browser will not let a page cancel them, and F6
 *  goes to the address bar. */
export const PAGE_KEYS: readonly string[] = [
  "F1",
  "F2",
  "F3",
  "F4",
  "F7",
  "F8",
  "F9",
  "F10",
];

let blocked = false;

/** True once a write has been refused -- a private window, or a browser
 *  set to block site data. The terminal keeps working; the shell says
 *  once that pages will not survive the tab. */
export function storageBlocked(): boolean {
  return blocked;
}

/** What is in storage, or an empty store. Store-level damage -- an
 *  envelope that will not parse, or an unknown version -- resets
 *  everything; page-level damage is answered in `dropPage`. */
export function readStore(): PageStore {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORE_KEY);
  } catch {
    blocked = true;
    return emptyStore();
  }
  if (raw === null) return emptyStore();
  try {
    const parsed: unknown = JSON.parse(raw);
    return isPageStore(parsed) ? parsed : emptyStore();
  } catch {
    return emptyStore();
  }
}

/** Writes the store back. False when storage refused it. */
export function writeStore(store: PageStore): boolean {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(store));
    return true;
  } catch {
    blocked = true;
    return false;
  }
}

/** One page, or null. */
export function readPage(name: string): Page | null {
  return readStore().pages[name] ?? null;
}

/** Saves a page, appending it to the key order the first time.
 *
 *  Order is append-only here: a saved page keeps the key it was given,
 *  because a reader learns "F2 is my options page" and a list that
 *  resorted itself on every save would make that untrue. */
export function savePage(page: Page): PageStore {
  const store = readStore();
  const order = store.order.includes(page.name) ? store.order : [...store.order, page.name];
  const next: PageStore = { ...store, order, pages: { ...store.pages, [page.name]: page } };
  writeStore(next);
  return next;
}

/** Forgets a page: the one that would not load, or the one a reader
 *  deleted. */
export function dropPage(name: string): PageStore {
  const store = readStore();
  const pages = { ...store.pages };
  delete pages[name];
  const next: PageStore = { ...store, order: store.order.filter((n) => n !== name), pages };
  writeStore(next);
  return next;
}

/** Renames a page, keeping its place in the key order. */
export function renamePage(from: string, to: string): PageStore {
  const store = readStore();
  const page = store.pages[from];
  if (page === undefined || from === to) return store;
  const pages = { ...store.pages };
  delete pages[from];
  pages[to] = { ...page, name: to };
  const next: PageStore = {
    ...store,
    order: store.order.map((name) => (name === from ? to : name)),
    pages,
  };
  writeStore(next);
  return next;
}

/** The page a function key opens, or null when that key has none. */
export function pageKeyName(store: PageStore, key: string): string | null {
  const index = PAGE_KEYS.indexOf(key);
  return index < 0 ? null : (store.order[index] ?? null);
}

/** Calls `fn` once the calls stop for `ms`.
 *
 *  dockview reports a layout change on every drag frame; writing each one
 *  would be a `JSON.stringify` of the whole page per animation frame. */
export function debounce<Args extends unknown[]>(
  ms: number,
  fn: (...args: Args) => void,
): ((...args: Args) => void) & { cancel: () => void } {
  let handle: ReturnType<typeof setTimeout> | null = null;
  const wrapped = (...args: Args): void => {
    if (handle !== null) clearTimeout(handle);
    handle = setTimeout(() => {
      handle = null;
      fn(...args);
    }, ms);
  };
  wrapped.cancel = () => {
    if (handle !== null) clearTimeout(handle);
    handle = null;
  };
  return wrapped;
}

//: Long enough that a drag writes once when it ends, short enough that a
//: reader who arranges a page and closes the tab keeps it.
export const SAVE_DEBOUNCE_MS = 250;
