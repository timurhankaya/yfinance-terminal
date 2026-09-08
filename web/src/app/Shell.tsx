import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactElement } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ApiError, getSymbol } from "../api/client";
import {
  DEFAULT_CODE,
  HOME_CODE,
  parse,
  ParseKind,
  pathToCommand,
  symbolCommand,
  SYMBOL_RE,
} from "../commands/parser";
import { getPanel, isMnemonic, listPanels } from "../commands/registry";
import type { Command, PanelArgs, PanelSpec } from "../commands/types";
import { useGo } from "../commands/go";
import { CommandPalette } from "./CommandPalette";
import { Workspace } from "./Workspace";
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

/** The symbol a market page inherits, carried in the history entry.
 *
 *  Not in the path, because the page is not about it; not in a store,
 *  because that would be a second source of truth the URL could disagree
 *  with. A history entry is exactly the right lifetime: Esc and forward
 *  restore the symbol that was on the strip at that step, and a link
 *  someone pastes carries none -- which is what a shared screener should
 *  carry. */
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
      title={panel.title}
      onClick={() => go({ symbol, code: panel.code, args: {} })}
    >
      {panel.code}
    </button>
  );
}


/** Everything a page is while it is on screen.
 *
 *  `panels` is what each panel is showing and `dock` is where they are;
 *  the two come from the same stored document and go back to it
 *  together, so there is no second copy to drift. `epoch` changes when a
 *  DIFFERENT page is loaded, which is what remounts the dock -- dockview
 *  reads a layout once, on the way up. */
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
  // `symbol` is absent on the market routes (`/ui/m/:code`, `/ui`);
  // present on `/ui/t/:symbol/:code`. Which of the two we are on is
  // therefore readable from the params alone.
  const { symbol: rawSymbol, code: rawCode, name: pageName } = useParams();
  const { search, state } = useLocation();
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
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  //: The dock itself, for the one question that is about pixels: which
  //: panel is to the left of this one.
  const apiRef = useRef<DockviewApi | null>(null);
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
      if (!saved) {
        if (!split) {
          go(next);
          return;
        }
        // An address page IS one panel, so splitting it is the moment it
        // stops being an address (spec, "Adres ve yönlendirme"). Both
        // panels ride in the history entry; the working page picks them
        // up from there.
        //
        // `replace`, because splitting is an edit rather than a step
        // (Karar 6): Esc goes back to wherever the reader was BEFORE this
        // page, not to the unsplit version of it, which they left on
        // purpose.
        //
        // Whatever the working page held is replaced, and no confirmation
        // is asked. `-` is the page with no name; naming one with
        // `PG SAVE` is how a page is kept, and a prompt on every split
        // would tax the common gesture to protect the page the terminal
        // calls scratch.
        void navigate(pagePath(PageName.Scratch), {
          replace: true,
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

  const urlPanels = useMemo<PanelSeed[]>(
    () => [{ id: "main", code: command.code, symbol: command.symbol, args: command.args, group: null }],
    [command],
  );
  const dockPanels = saved ? pagePanels : urlPanels;

  /** What the shell's own controls are about.
   *
   *  On a page that is an address, that is the address. On a saved page
   *  it is whichever panel has the keyboard: typing `GIP` there means
   *  "this panel, intraday", and the strip and the function bar are about
   *  the same panel the command box is. */
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
        setWarning("GRP works on a page: press Ctrl+Enter to open a second panel first");
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

  /** Hands the keyboard to the panel in that direction.
   *
   *  dockview has no directional navigation, so this is worked out from
   *  where the panels are: each group's rectangle, and the arithmetic in
   *  `pickNeighbour`. A group is one box however many tabs it holds --
   *  the tabs are stacked in the same place, so "left" cannot mean one of
   *  them. */
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
          setWarning("There is no page to save yet: split this one with Ctrl+Enter first.");
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
    [here, runHere, runGroup, runPage, runShare, known, focusedPanel, offer, navigate],
  );

  // Focus the box once, on mount: the shell owns the keyboard from the
  // first paint, and a later re-render must not steal focus back from a
  // panel or the palette.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  function onPick(text: string) {
    const pending = pendingRef.current;
    pendingRef.current = null;
    setPalette({ open: false, query: "" });
    if (pending && SYMBOL_RE.test(text.toUpperCase()) && !isMnemonic(text)) {
      // The palette's symbol results came straight from the API, so there is
      // no need to re-validate them the way a typed symbol is.
      setWarning(null);
      setDraft("");
      runHere({ symbol: text.toUpperCase(), code: pending.code, args: pending.args });
      return;
    }
    void submit(text);
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

  const hasSymbol = here.symbol !== null;
  const panels = listPanels();
  const market_panels = panels.filter((panel) => !panel.needsSymbol);
  const symbol_panels = panels.filter((panel) => panel.needsSymbol);

  return (
    <div className="shell">
      <header className="command-bar">
        <div className="command-row">
          <input
            ref={inputRef}
            aria-label="command"
            placeholder="Symbol, function, args… Enter to run"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="accent" role="group" aria-label="accent colour">
            {ACCENTS.map((choice) => (
              <button
                key={choice}
                type="button"
                className={`accent-swatch accent-${choice}`}
                aria-label={choice}
                aria-pressed={choice === accent}
                title={`Accent: ${choice}`}
                onClick={() => setAccent(choice)}
              />
            ))}
          </div>
        </div>
        {warning && <p className="warn">{warning}</p>}
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
      {/* Two groups, because they are two kinds of page and the URL now
          says so: a market page has no symbol in its address, a symbol
          page does. A flat list of 22 codes gave a reader no way to tell
          which of them a symbol was even relevant to. */}
      <nav className="fnbar" aria-label="functions">
        <span className="fn-group">Market</span>
        {market_panels.map((panel) => (
          <FnButton key={panel.code} panel={panel} current={here.code} go={runHere} symbol={command.symbol} runnable />
        ))}
        <span className="fn-group">
          {here.symbol === null ? "This symbol" : here.symbol}
        </span>
        {symbol_panels.map((panel) => (
          <FnButton
            key={panel.code}
            panel={panel}
            current={here.code}
            go={runHere}
            symbol={here.symbol}
            runnable={hasSymbol}
          />
        ))}
      </nav>
      <main className="panel">
        <Workspace
          // A different page is a different dock: dockview reads a layout
          // once, on the way up, so restoring one means building it.
          key={page.epoch}
          panels={dockPanels}
          onRun={onRun}
          onActive={setActiveId}
          groups={groups}
          initial={page.dock}
          onLayout={saved ? onLayout : undefined}
          onLayoutError={onLayoutError}
          onApi={(api) => (apiRef.current = api)}
        />
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
        <span className="muted">Not affiliated with, endorsed by or connected to Yahoo.</span>
        <span className="credits-right">
          Powered by{" "}
          <a href="https://monafy.com/" target="_blank" rel="noopener noreferrer">
            monafy.com
          </a>{" "}
          ·{" "}
          <a href="https://github.com/kayacekovic" target="_blank" rel="noopener noreferrer">
            <img className="credit-logo" src="https://github.com/favicon.ico" alt="" />
            Timurhan Kaya
          </a>
        </span>
      </footer>
      <CommandPalette
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
