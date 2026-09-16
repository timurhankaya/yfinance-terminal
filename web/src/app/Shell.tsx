import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactElement } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ApiError, getSymbol } from "../api/client";
import {
  DEFAULT_CODE,
  commandToPath,
  HOME_CODE,
  parse,
  ParseKind,
  pathToCommand,
  symbolCommand,
} from "../commands/parser";
import { getPanel, listPanels } from "../commands/registry";
import type { Command, PanelArgs, PanelSpec } from "../commands/types";
import { useGo } from "../commands/go";
import { CommandPalette } from "./CommandPalette";
import { SinglePanel, Workspace } from "./Workspace";
import type { PanelSeed } from "./Workspace";
import {
  GROUP_DETACH,
  GRP_CODE,
  PG_CODE,
  PG_SAVE,
  SHARE_CODE,
} from "../workspace/actions";
import { canJoinGroup, parseGroup, symbolFor } from "../workspace/groups";
import type { GroupSymbols } from "../workspace/groups";
import { normalizeName, pagePath } from "../workspace/PG";
import { PageName, decodePage, encodePage, seedsFromDock } from "../workspace/page";
import type { Page } from "../workspace/page";
import {
  SAVE_DEBOUNCE_MS,
  debounce,
  dropPage,
  pageKeyName,
  readPage,
  readStore,
  savePage,
} from "../workspace/store";
import type { DockviewApi, SerializedDockview } from "dockview-react";
import { Direction, pickNeighbour } from "../workspace/neighbour";
import type { Box } from "../workspace/neighbour";
import { useGlobalKeys } from "./keys";
import { ACCENTS, applyAccent, readAccent } from "./accent";
import type { Accent } from "./accent";
import { TooltipLayer } from "./TooltipLayer";
import { functionHelp } from "./tab-help";
import { dockviewEnabled } from "./config";

/** The symbol a market page inherits, carried in the history entry:
 *  not in the path (the page is not about it), not in a store (a second
 *  source of truth the URL could disagree with). Esc and forward restore
 *  it per step, and a pasted link carries none. */
interface HistoryContext {
  symbol?: string | null;
  /** Panels a split handed over on the way from an address to the
   *  working page. The history entry is the right lifetime for them for
   *  the same reason it is right for the symbol: they belong to this
   *  step and to no other. */
  panels?: PanelSeed[];
}

function FnButton(props: {
  panel: PanelSpec;
  current: string;
  symbol: string | null;
  runnable: boolean;
  go: (command: Command) => void;
}): ReactElement {
  const { panel, current, symbol, runnable, go } = props;
  return (
    <button
      type="button"
      className={panel.code === current ? "fn fn-active" : "fn"}
      aria-current={panel.code === current ? "page" : undefined}
      disabled={!runnable}
      data-tooltip={functionHelp(panel)}
      onClick={() => go({ symbol, code: panel.code, args: {} })}
    >
      {panel.code}
    </button>
  );
}


/** Everything a page is while it is on screen. `panels` and `dock` come
 *  from the same stored document and go back to it together. `epoch`
 *  changes when a DIFFERENT page is loaded, which remounts the dock --
 *  dockview reads a layout once, on the way up. */
interface PageState {
  name: string;
  panels: PanelSeed[];
  groups: GroupSymbols;
  activeId: string | null;
  /** The layout to restore, when this page came from the store or a link. */
  dock?: SerializedDockview;
  epoch: number;
}

//: A page nobody has arranged yet.
function blankPage(name: string, epoch: number): PageState {
  return {
    name,
    panels: [{ id: "home-1", code: HOME_CODE, symbol: null, args: {}, group: null }],
    groups: {},
    activeId: "home-1",
    epoch,
  };
}

function fromPage(page: Page, epoch: number): PageState {
  const panels = seedsFromDock(page.dock);
  return {
    name: page.name,
    panels: panels.length > 0 ? panels : blankPage(page.name, epoch).panels,
    groups: page.groups,
    activeId: panels[0]?.id ?? "home-1",
    dock: panels.length > 0 ? page.dock : undefined,
    epoch,
  };
}

/** The page an address means: the one in the store, or a blank one.
 *
 *  A link is deliberately NOT read here. It may need to be offered
 *  rather than applied, and that is a question with an answer only the
 *  effect below can wait for. */
function loadPage(name: string | undefined, epoch = 0): PageState {
  if (name === undefined) return blankPage(PageName.Scratch, epoch);
  const stored = readPage(name);
  return stored === null ? blankPage(name, epoch) : fromPage(stored, epoch);
}

/** The counter in a panel id, or 0 for one shaped differently. */
function idNumber(seed: PanelSeed): number {
  const parsed = Number.parseInt(seed.id.slice(seed.id.lastIndexOf("-") + 1), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** `gip-3`: the code and a counter.
 *
 *  Not a uuid. A shared link carries every id in it, and `SHARE`'s
 *  character budget is spent on layout rather than on 36 characters of
 *  randomness per panel. */
function panelId(code: string, counter: { current: number }): string {
  return `${code.toLowerCase()}-${counter.current++}`;
}

//: The query parameter a shared page arrives in.
export const SHARE_PARAM = "l";

export function Shell() {
  const docking = dockviewEnabled();
  // `symbol` is absent on the market routes (`/ui/m/:code`, `/ui`);
  // present on `/ui/t/:symbol/:code`. Which of the two we are on is
  // therefore readable from the params alone.
  const { symbol: rawSymbol, code: rawCode, name: pageName } = useParams();
  const { pathname, search, state } = useLocation();
  const navigate = useNavigate();
  const go = useGo();
  // A saved page is the one kind of page whose panels are not in its
  // address: a grid of four does not fit in one, which is the whole
  // reason the store exists.
  const saved = pageName !== undefined;
  const [page, setPage] = useState<PageState>(() => loadPage(pageName));
  const { panels: pagePanels, groups, activeId } = page;
  const nextId = useRef(1);
  // dockview's own document for the page on screen, kept so that a
  // change to the LETTERS -- which are not in it -- can be saved without
  // waiting for the next drag.
  const dockRef = useRef<SerializedDockview | null>(null);
  const market = rawSymbol === undefined;
  const code = rawCode ?? (market ? HOME_CODE : DEFAULT_CODE);
  const context = (state as HistoryContext | null)?.symbol ?? null;
  // The URL is the one input a panel gets that `parseArgs` never saw, so
  // the panel's own rules are applied to it here -- once, for every
  // panel, rather than in each panel that remembered to.
  const command = useMemo(() => {
    const fromPath = pathToCommand(rawSymbol ?? null, code, search);
    // A market page still gets the strip's symbol as a prop: `DS` filters
    // a symbol-scoped dataset to it and `WLA` opens on it. What it does
    // not get is that symbol in its own address.
    const withContext = market ? { ...fromPath, symbol: context } : fromPath;
    const normalize = getPanel(withContext.code)?.normalizeArgs;
    return normalize === undefined
      ? withContext
      : { ...withContext, args: normalize(withContext.args) };
  }, [rawSymbol, code, search, market, context]);
  // Normalise deep links without adding a history entry. Only arguments
  // present in the URL are retained, so default tabs need no query string.
  useEffect(() => {
    if (saved || !getPanel(command.code)) return;
    const args = Object.fromEntries([...new URLSearchParams(search)].flatMap(([key, value]) => {
      const normalized = command.args[key];
      return value !== "" && normalized !== undefined ? [[key, normalized]] : [];
    }));
    const canonical = commandToPath({ ...command, args });
    if (canonical !== `${pathname}${search}`) void navigate(canonical, { replace: true, state: { symbol: command.symbol } });
  }, [saved, command, pathname, search, navigate]);

  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  //: The dock itself, for the one question that is about pixels: which
  //: panel is to the left of this one.
  const apiRef = useRef<DockviewApi | null>(null);
  useEffect(() => { if (!saved) apiRef.current = null; }, [saved]);
  const [accent, setAccent] = useState<Accent>(() => readAccent());

  // The document carries the choice; the dock is rebuilt so that what is
  // drawn on a canvas -- a chart's own line -- is redrawn in it, rather
  // than staying the old colour until the panel happens to update.
  useEffect(() => {
    applyAccent(accent);
  }, [accent]);
  /** A page that arrived in a link and is waiting to be let in, because
   *  the working page already has something on it. */
  const [offer, setOffer] = useState<Page | null>(null);
  /** The link `SHARE` wrote, shown under the command box until the next
   *  command. */
  const [shareLink, setShareLink] = useState<string | null>(null);
  const [palette, setPalette] = useState<{ open: boolean; query: string }>({ open: false, query: "" });
  const pendingRef = useRef<{ code: string; args: PanelArgs } | null>(null);

  /** Runs a command from the shell's own controls -- the command box, the
   *  function bar, `?`. On a page that *is* an address that means going
   *  there; on a saved page it means running in the panel the keyboard is
   *  talking to, or opening a new one beside it. */
  const runHere = useCallback(
    (next: Command, split = false) => {
      split = split && dockviewEnabled();
      if (!saved) {
        if (!split) {
          go(next);
          return;
        }
        // An address page IS one panel, so splitting it is the moment it
        // stops being an address. Both panels ride in the history entry
        // so Back returns to the single page. The scratch page `-` is
        // replaced without confirmation: `PG SAVE` is how a page is kept.
        void navigate(pagePath(PageName.Scratch), {
          state: {
            panels: [
              // `command`, not `here`: on a page that is an address the
              // two are the same thing, and this one is what the address
              // itself parsed to.
              { id: panelId(command.code, nextId), ...command, group: null },
              { id: panelId(next.code, nextId), ...next, group: null },
            ],
          } satisfies HistoryContext,
        });
        return;
      }
      if (split) {
        const id = panelId(next.code, nextId);
        setPage((current) => ({ ...current, panels: [...current.panels, { id, ...next, group: null }] }));
        return;
      }
      setPage((current) => ({
        ...current,
        panels: current.panels.map((panel) =>
          // A panel keeps its letter when its content changes: `GIP` in a
          // group-B panel is still B's.
          panel.id === current.activeId ? { id: panel.id, group: panel.group, ...next } : panel,
        ),
      }));
    },
    [saved, go, navigate, command],
  );

  /** A panel ran a command: it replaces itself, wherever it is. */
  const onRun = useCallback(
    (id: string, next: Command) => {
      if (!saved) {
        go(next);
        return;
      }
      setPage((current) => ({
        ...current,
        panels: current.panels.map((panel) =>
          panel.id === id ? { id, group: panel.group, ...next } : panel,
        ),
      }));
    },
    [saved, go],
  );

  const setActiveId = useCallback((id: string | null) => {
    setPage((current) => (current.activeId === id ? current : { ...current, activeId: id }));
  }, []);

  // A different address is a different page: read it, and give the dock a
  // new `epoch` so it is rebuilt from the layout rather than being asked
  // to morph into it.
  const epoch = useRef(0);
  const applyPage = useCallback((loaded: PageState) => {
    dockRef.current = loaded.dock ?? null;
    // Ids from a stored page are `code-n`; the counter has to clear the
    // highest of them or the next split would collide with one.
    nextId.current = 1 + loaded.panels.reduce((top, seed) => Math.max(top, idNumber(seed)), 0);
    setPage(loaded);
  }, []);

  useEffect(() => {
    if (!saved || pageName === undefined) return;
    const carried = (state as HistoryContext | null)?.panels;
    if (carried !== undefined && carried.length > 0) {
      epoch.current += 1;
      applyPage({
        name: pageName,
        panels: carried,
        groups: {},
        // The new panel has the keyboard, not the one that was split
        // (Karar 6): opening does not enter history, moving the focus
        // does, and this entry is the new panel's.
        activeId: carried[carried.length - 1]?.id ?? null,
        epoch: epoch.current,
      });
      return;
    }
    const encoded = new URLSearchParams(search).get(SHARE_PARAM);
    if (encoded !== null) {
      const link = decodePage(encoded);
      if (link === null) {
        // The working page is left exactly as it was: a link that will
        // not decode is not a reason to throw away what is on screen.
        setWarning("That link does not carry a page.");
        void navigate(pagePath(pageName), { replace: true });
        return;
      }
      // A link is only let in over an empty page. Anywhere else it is an
      // offer, because the arrangement it would replace took work.
      const current = readPage(pageName);
      if (current !== null && seedsFromDock(current.dock).length > 0) {
        setOffer({ ...link, name: pageName });
        return;
      }
      // Stored, then navigated to: the page comes back through the same
      // door every other page does, and the parameter is spent -- a link
      // left in the address would be re-applied by the next Back, over
      // whatever had been done since.
      savePage({ ...link, name: pageName });
      void navigate(pagePath(pageName), { replace: true });
      return;
    }
    epoch.current += 1;
    applyPage(loadPage(pageName, epoch.current));
  }, [saved, pageName, search, state, applyPage, navigate]);

  /** Writes the page back, at most once per quarter second.
   *
   *  dockview reports a layout change on every frame of a drag; a
   *  `JSON.stringify` of the whole page per frame is a cost with nothing
   *  to show for it. */
  const persist = useMemo(
    () =>
      debounce(SAVE_DEBOUNCE_MS, (next: Page) => {
        savePage(next);
      }),
    [],
  );
  useEffect(() => () => persist.cancel(), [persist]);

  const onLayout = useCallback(
    (dock: SerializedDockview) => {
      dockRef.current = dock;
      persist({ name: pageName ?? PageName.Scratch, groups, dock });
    },
    [persist, pageName, groups],
  );

  // The letters are a page-level fact and are not in dockview's document,
  // so a change to them has to be written on its own rather than waiting
  // for the next drag.
  useEffect(() => {
    if (!saved) return;
    const dock = dockRef.current;
    if (dock === null) return;
    persist({ name: pageName ?? PageName.Scratch, groups, dock });
  }, [saved, pageName, groups, persist]);

  /** A stored layout dockview refused. The page is dropped rather than
   *  left to fail on every visit; what it held is not recoverable and was
   *  never data, only an arrangement. */
  const onLayoutError = useCallback(() => {
    const name = pageName ?? PageName.Scratch;
    dropPage(name);
    dockRef.current = null;
    setWarning(`${name} could not be restored and has been forgotten.`);
  }, [pageName]);

  //: One panel on screen: the reader has not met the dock yet, so the
  //: command box says how. `activeId` is dockview's, so this follows the
  //: page as it is, not as it was seeded.
  const alone = !saved || page.panels.length <= 1;

  /** What the shell's own controls are about: the address on an address
   *  page, otherwise whichever panel has the keyboard -- the strip, the
   *  function bar and the command box all mean the same panel. */
  const focusedPanel = saved ? pagePanels.find((panel) => panel.id === activeId) : undefined;
  // Memoised because half a dozen callbacks depend on it: rebuilt every
  // render, every one of them would be a new function every render too,
  // and the effects that hold them would re-run for nothing.
  const here: Command = useMemo(
    () =>
      focusedPanel === undefined
        ? command
        : {
            symbol: symbolFor(groups, focusedPanel.group, focusedPanel.symbol),
            code: focusedPanel.code,
            args: focusedPanel.args,
          },
    [focusedPanel, command, groups],
  );

  /** Pins the focused panel to a letter, or takes its letter away.
   *
   *  A letter only means something on a panel that is about one symbol,
   *  and it says so rather than silently doing nothing. */
  const runGroup = useCallback(
    (tokens: string[]) => {
      const token = tokens[0];
      if (token === undefined) {
        setWarning("GRP needs a letter: GRP B, or GRP - to unpin");
        return;
      }
      if (focusedPanel === undefined) {
        setWarning("GRP works on a page: press + to open a workspace first");
        return;
      }
      const spec = getPanel(focusedPanel.code);
      const group = token === GROUP_DETACH ? null : parseGroup(token);
      if (group === null && token !== GROUP_DETACH) {
        setWarning(`${token.toUpperCase()} is not a group letter (A-G)`);
        return;
      }
      if (group !== null && !canJoinGroup(spec)) {
        setWarning(`${focusedPanel.code} carries its own symbols, so a letter cannot speak for it`);
        return;
      }
      setWarning(null);
      setDraft("");
      setPage((current) => ({
        ...current,
        panels: current.panels.map((panel) =>
          panel.id !== focusedPanel.id
            ? panel
            : {
                ...panel,
                group,
                // Unpinning keeps what the panel was showing: the letter
                // goes, the symbol on screen does not.
                symbol:
                  group === null ? symbolFor(current.groups, panel.group, panel.symbol) : panel.symbol,
              },
        ),
      }));
    },
    [focusedPanel],
  );

  /** Hands the keyboard to the panel in that direction. dockview has no
   *  directional navigation, so it is worked out from each group's
   *  rectangle (`pickNeighbour`); a group is one box however many tabs
   *  it holds. */
  const onMoveFocus = useCallback((direction: Direction) => {
    const api = apiRef.current;
    if (api === null) return;
    const boxes: Box[] = [];
    for (const group of api.groups) {
      const id = group.activePanel?.id;
      if (id === undefined) continue;
      const rect = group.element.getBoundingClientRect();
      boxes.push({ id, left: rect.left, top: rect.top, width: rect.width, height: rect.height });
    }
    const next = pickNeighbour(boxes, api.activePanel?.id ?? "", direction);
    if (next !== null) api.getPanel(next)?.api.setActive();
  }, []);

  /** `PG`, `PG SAVE <name>`, `PG <name>`.
   *
   *  Saving is the shell's job rather than the panel's: the thing being
   *  saved is the layout the panel is sitting inside, and opening one
   *  changes what the whole window is. */
  const runPage = useCallback(
    (tokens: string[]) => {
      setShareLink(null);
      const [first, second] = tokens;
      if (first === undefined) {
        runHere({ symbol: here.symbol, code: PG_CODE, args: {} });
        setDraft("");
        return;
      }
      if (first.toUpperCase() === PG_SAVE) {
        const name = second === undefined ? null : normalizeName(second);
        if (name === null) {
          setWarning(`A page name is letters, digits and dashes: PG ${PG_SAVE} trading`);
          return;
        }
        const dock = dockRef.current;
        if (!saved || dock === null) {
          setWarning("There is no page to save yet: press + to open a workspace first.");
          return;
        }
        // Naming the working page MOVES it. Two things would otherwise
        // leave `-` behind: the copy itself, and the write already
        // scheduled under the old name, which lands a quarter second
        // later and puts the working page back.
        persist.cancel();
        savePage({ name, groups, dock });
        if (pageName === PageName.Scratch) dropPage(PageName.Scratch);
        setWarning(null);
        setDraft("");
        // The name IS the address, so saving moves there: a page called
        // `trading` that stayed on `/ui/w/-` would not be shareable or
        // reloadable, which is what naming it was for.
        void navigate(pagePath(name));
        return;
      }
      const name = normalizeName(first);
      if (name === null || readPage(name) === null) {
        setWarning(`No page called ${first}.`);
        runHere({ symbol: here.symbol, code: PG_CODE, args: {} });
        return;
      }
      setWarning(null);
      setDraft("");
      void navigate(pagePath(name));
    },
    [runHere, here.symbol, saved, groups, navigate, persist, pageName],
  );

  /** `SHARE`: the page as a link, written under the command box.
   *
   *  Nothing is sent anywhere. The terminal is public and has no user to
   *  own a stored page, so the layout travels inside the address itself. */
  const runShare = useCallback(() => {
    const dock = dockRef.current;
    if (!saved || dock === null) {
      setWarning("SHARE works on a page: this address already is its own link.");
      return;
    }
    const encoded = encodePage({ name: pageName ?? PageName.Scratch, groups, dock });
    if (encoded === null) {
      setWarning("This page is too big to put in a link. Close a panel or two and try again.");
      return;
    }
    setWarning(null);
    setDraft("");
    setShareLink(
      `${window.location.origin}${pagePath(PageName.Scratch)}?${SHARE_PARAM}=${encoded}`,
    );
  }, [saved, pageName, groups]);

  /** Checks a symbol against the archive, opening the palette when it is
   *  not there. `false` means the caller should stop. */
  const known = useCallback(
    async (symbol: string, pending: { code: string; args: PanelArgs }): Promise<boolean> => {
      try {
        await getSymbol(symbol);
        return true;
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          setWarning(`No such symbol ${symbol}`);
          pendingRef.current = pending;
          setPalette({ open: true, query: symbol });
          return false;
        }
        setWarning("Could not reach the API. Try again.");
        return false;
      }
    },
    [],
  );

  const submit = useCallback(
    async (text: string, split = false) => {
      split = split && saved;
      const result = parse(text, { symbol: here.symbol, code: here.code });
      if (result.kind === ParseKind.Empty) {
        // Enter on an empty box does nothing -- except answer the one
        // question the shell ever asks, which is whether a link may
        // replace the page that is already there.
        if (offer !== null) {
          savePage(offer);
          setOffer(null);
          setWarning(null);
          void navigate(pagePath(offer.name), { replace: true });
        }
        return;
      }
      setShareLink(null);
      if (result.kind === ParseKind.Error) {
        setWarning(result.message);
        return;
      }
      if (result.kind === ParseKind.Action) {
        if (!dockviewEnabled()) {
          setWarning("Workspaces are disabled for this terminal.");
          return;
        }
        if (result.code === GRP_CODE) runGroup(result.tokens);
        else if (result.code === PG_CODE) runPage(result.tokens);
        else if (result.code === SHARE_CODE) runShare();
        return;
      }

      // A bare ticker: where a letter claims the focused panel it moves
      // the letter -- every panel wearing it follows -- and where none
      // does it is what it always was, this panel on another symbol.
      if (result.kind === ParseKind.Symbol) {
        const group = focusedPanel?.group ?? null;
        const fallback = symbolCommand(result.symbol, here.code);
        if (group === null && fallback.kind === ParseKind.Error) {
          setWarning(fallback.message);
          return;
        }
        const pending =
          fallback.kind === ParseKind.Command
            ? { code: fallback.command.code, args: fallback.command.args }
            : { code: here.code, args: {} };
        if (!(await known(result.symbol, pending))) return;
        setWarning(null);
        setDraft("");
        inputRef.current?.blur();
        if (group !== null) {
          setPage((current) => ({
            ...current,
            groups: { ...current.groups, [group]: result.symbol },
          }));
          return;
        }
        if (fallback.kind === ParseKind.Command) runHere(fallback.command, split);
        return;
      }

      const next = result.command;
      if (next.symbol && next.symbol !== here.symbol) {
        if (!(await known(next.symbol, { code: next.code, args: next.args }))) return;
      }
      setWarning(null);
      setDraft("");
      // Hand the keyboard to the panel: while the box keeps focus, j/k/Enter
      // would be typed into it instead of moving a list selection.
      inputRef.current?.blur();
      runHere(next, split);
    },
    [here, runHere, runGroup, runPage, runShare, known, focusedPanel, offer, navigate, saved],
  );

  // Focus the box once, on mount: the shell owns the keyboard from the
  // first paint, and a later re-render must not steal focus back from a
  // panel or the palette.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  function onPick(text: string, split = false, kind: "symbol" | "function" = "function") {
    split = split && saved;
    const pending = pendingRef.current;
    pendingRef.current = null;
    setPalette({ open: false, query: "" });
    if (kind === "symbol") {
      // Search results are already resolved identities. A ticker such as PG
      // or CF must never be reinterpreted as a terminal function.
      const symbol = text.toUpperCase();
      setWarning(null);
      setDraft("");
      const group = focusedPanel?.group ?? null;
      if (!pending && group !== null && !split) {
        setPage((current) => ({ ...current, groups: { ...current.groups, [group]: symbol } }));
        return;
      }
      const fallback = symbolCommand(symbol, here.code);
      if (pending) runHere({ symbol, code: pending.code, args: pending.args }, split);
      else if (fallback.kind === ParseKind.Command) runHere(fallback.command, split);
      return;
    }
    void submit(text, split);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      // Ctrl/Meta+Enter opens the result beside the panel instead of in
      // it; on a page that is an address there is nowhere to put it.
      void submit(draft, event.ctrlKey || event.metaKey);
      return;
    }
    if (event.key === "Escape" && !event.shiftKey) {
      // Stop this Escape here: clear+blur is the command box's own
      // behaviour, and the global listener in keys.ts would otherwise see
      // the same keypress (it bubbles to window) and navigate back too.
      event.stopPropagation();
      setDraft("");
      event.currentTarget.blur();
    }
  }

  const openHelp = useCallback(() => {
    runHere({ symbol: here.symbol, code: "HELP", args: {} });
  }, [runHere, here.symbol]);

  /** F1-F4, F7-F10: the saved page in that position. */
  const onPageKey = useCallback(
    (key: string) => {
      if (!dockviewEnabled()) return;
      const name = pageKeyName(readStore(), key);
      if (name === null) {
        setWarning(`${key} has no page yet. Arrange one and type PG ${PG_SAVE} <name>.`);
        return;
      }
      setWarning(null);
      void navigate(pagePath(name));
    },
    [navigate],
  );

  useGlobalKeys({ inputRef, paletteOpen: palette.open, openHelp, onPageKey, onMoveFocus });

  const market_panels = listPanels().filter((panel) => !panel.needsSymbol && panel.code !== PG_CODE);

  return (
    <div className="shell">
      <header className="command-bar">
        <div className="command-row">
          <span className="terminal-brand">YFIN<span>TERMINAL</span></span>
          <span className="command-prompt" aria-hidden="true">›</span>
          <input
            ref={inputRef}
            aria-label="command"
            placeholder="Symbol, function, args… Enter to run"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <button type="button" className="search-trigger" aria-label="Search symbols and functions" data-tooltip="Search · Find a ticker, company name or terminal function." onClick={() => { pendingRef.current = null; setPalette({ open: true, query: draft.trim() }); }}>
            <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" strokeWidth="1.6" /><path d="m12 12 5 5" stroke="currentColor" strokeWidth="1.6" /></svg>
            Search
          </button>
          <div className="accent" role="group" aria-label="accent colour">
            <span className="accent-label">Theme</span>
            {ACCENTS.map((choice) => (
              <button
                key={choice}
                type="button"
                className={`accent-swatch accent-${choice}`}
                aria-label={choice}
                aria-pressed={choice === accent}
                data-tooltip={`Theme · ${choice === "amber" ? "Yellow / amber" : choice === "violet" ? "Purple / violet" : "Blue"} accent. Saved automatically.`}
                onClick={() => setAccent(choice)}
              >
                <span aria-hidden="true">{choice === "amber" ? "Yellow" : choice === "violet" ? "Purple" : "Blue"}</span>
              </button>
            ))}
          </div>
        </div>
        {warning && <p className="warn">{warning}</p>}
        {alone && docking && (
          // Said once, where the reader is typing, and only while there
          // is one panel: after that the page has shown them.
          <p className="muted hint">
            <code className="usage">{saved ? "Ctrl+Enter" : "+"}</code> opens a second panel beside this one
          </p>
        )}
        {offer !== null && (
          <p className="warn">
            This link carries a page. Enter to replace the working page — anything else leaves it
            alone.
          </p>
        )}
        {shareLink !== null && (
          // Written out rather than copied to the clipboard: a terminal
          // that silently took the clipboard would be doing something the
          // reader did not ask for, and this is selectable.
          <p className="share">{shareLink}</p>
        )}
      </header>
      {/* The market pages only; the symbol functions live in each symbol
          panel's band (`SymbolBand.tsx`), since a split page shows more
          than one symbol. */}
      <nav className="fnbar" aria-label="functions">
        <span className="fn-group">Market</span>
        {market_panels.map((panel) => (
          <FnButton key={panel.code} panel={panel} current={here.code} go={runHere} symbol={command.symbol} runnable />
        ))}
      </nav>
      <main className="panel">
        {saved ? <Workspace
          // A different page is a different dock: dockview reads a layout
          // once, on the way up, so restoring one means building it.
          key={page.epoch}
          panels={pagePanels}
          onRun={onRun}
          onActive={setActiveId}
          groups={groups}
          initial={page.dock}
          onLayout={saved ? onLayout : undefined}
          onLayoutError={onLayoutError}
          onApi={(api) => (apiRef.current = api)}
          onSplit={(command) => runHere(command, true)}
        /> : <SinglePanel command={command} onRun={go} onSplit={docking ? (command) => runHere(command, true) : undefined} />}
      </main>
      <footer className="credits" aria-label="credits">
        <span>Data</span>
        <a href="https://finance.yahoo.com/" target="_blank" rel="noopener noreferrer">
          <img className="credit-logo" src="https://s.yimg.com/rz/l/favicon.ico" alt="" />
          Yahoo Finance
        </a>
        <span>via</span>
        <a href="https://github.com/ranaroussi/yfinance" target="_blank" rel="noopener noreferrer">
          <img
            className="credit-logo credit-logo-wide"
            src="https://raw.githubusercontent.com/ranaroussi/yfinance/main/doc/yfinance-gh-logo-dark.webp"
            alt=""
          />
          yfinance
        </a>
        <span className="muted">
          Not affiliated with, endorsed by or connected to Yahoo. Data is subject to{" "}
          <a href="https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html" target="_blank" rel="noopener noreferrer">
            Yahoo&apos;s terms
          </a>
          ; this software does not license it.
        </span>
        <span className="credits-right">
          Powered by{" "}
          <a href="https://monafy.com/" target="_blank" rel="noopener noreferrer">
            monafy.com
          </a>{" "}
          ·{" "}
          <a href="https://github.com/kayacekovic" target="_blank" rel="noopener noreferrer">
            <svg className="credit-logo credit-github" viewBox="0 0 24 24" aria-hidden="true">
              <path fill="currentColor" d="M12 .75a11.25 11.25 0 0 0-3.558 21.923c.563.104.768-.244.768-.542 0-.267-.01-.975-.015-1.913-3.13.68-3.79-1.508-3.79-1.508-.512-1.3-1.25-1.646-1.25-1.646-1.023-.7.078-.686.078-.686 1.13.08 1.725 1.16 1.725 1.16 1.006 1.723 2.64 1.225 3.283.937.102-.73.394-1.226.716-1.508-2.498-.284-5.124-1.249-5.124-5.56 0-1.23.44-2.233 1.16-3.02-.117-.285-.503-1.43.11-2.98 0 0 .945-.303 3.094 1.154A10.78 10.78 0 0 1 12 6.18c.955.005 1.916.129 2.813.379 2.148-1.457 3.09-1.154 3.09-1.154.616 1.55.23 2.695.113 2.98.722.787 1.158 1.79 1.158 3.02 0 4.322-2.63 5.272-5.136 5.55.404.35.766 1.042.766 2.1 0 1.517-.014 2.74-.014 3.113 0 .3.203.65.774.54A11.252 11.252 0 0 0 12 .75Z" />
            </svg>
            Timurhan Kaya
          </a>
        </span>
      </footer>
      <TooltipLayer />
      <CommandPalette
        canSplit={saved}
        open={palette.open}
        query={palette.query}
        onQuery={(query) => setPalette((p) => ({ ...p, query }))}
        onPick={onPick}
        onClose={() => {
          pendingRef.current = null;
          setPalette({ open: false, query: "" });
        }}
      />
    </div>
  );
}
