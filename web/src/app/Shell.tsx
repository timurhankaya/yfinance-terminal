import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactElement } from "react";
import { useLocation, useParams } from "react-router";
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
import { GROUP_DETACH, GRP_CODE } from "../workspace/actions";
import { canJoinGroup, parseGroup, symbolFor } from "../workspace/groups";
import type { GroupSymbols } from "../workspace/groups";
import { useGlobalKeys } from "./keys";

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

export function Shell() {
  // `symbol` is absent on the market routes (`/ui/m/:code`, `/ui`);
  // present on `/ui/t/:symbol/:code`. Which of the two we are on is
  // therefore readable from the params alone.
  const { symbol: rawSymbol, code: rawCode, name: pageName } = useParams();
  const { search, state } = useLocation();
  const go = useGo();
  // A saved page is the one kind of page whose panels are not in its
  // address. Until 2a-3 gives it a store they live here, so a reload
  // starts it over -- which is the honest state of a page that has not
  // been given anywhere to be saved yet.
  const saved = pageName !== undefined;
  const [pagePanels, setPagePanels] = useState<PanelSeed[]>(() => [
    { id: "p1", code: HOME_CODE, symbol: null, args: {}, group: null },
  ]);
  // What each letter is pointed at. A page-level fact: every panel
  // wearing the letter reads the same symbol from here.
  const [groups, setGroups] = useState<GroupSymbols>({});
  const [activeId, setActiveId] = useState<string | null>("p1");
  const nextId = useRef(2);
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
  const [palette, setPalette] = useState<{ open: boolean; query: string }>({ open: false, query: "" });
  const pendingRef = useRef<{ code: string; args: PanelArgs } | null>(null);

  /** Runs a command from the shell's own controls -- the command box, the
   *  function bar, `?`. On a page that *is* an address that means going
   *  there; on a saved page it means running in the panel the keyboard is
   *  talking to, or opening a new one beside it. */
  const runHere = useCallback(
    (next: Command, split = false) => {
      if (!saved) {
        go(next);
        return;
      }
      if (split) {
        const id = `p${nextId.current++}`;
        setPagePanels((current) => [...current, { id, ...next, group: null }]);
        return;
      }
      setPagePanels((current) =>
        current.map((panel) =>
          // A panel keeps its letter when its content changes: `GIP` in a
          // group-B panel is still B's.
          panel.id === activeId ? { id: panel.id, group: panel.group, ...next } : panel,
        ),
      );
    },
    [saved, go, activeId],
  );

  /** A panel ran a command: it replaces itself, wherever it is. */
  const onRun = useCallback(
    (id: string, next: Command) => {
      if (!saved) {
        go(next);
        return;
      }
      setPagePanels((current) =>
        current.map((panel) => (panel.id === id ? { id, group: panel.group, ...next } : panel)),
      );
    },
    [saved, go],
  );

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
  const here: Command =
    focusedPanel === undefined
      ? command
      : {
          symbol: symbolFor(groups, focusedPanel.group, focusedPanel.symbol),
          code: focusedPanel.code,
          args: focusedPanel.args,
        };

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
      setPagePanels((current) =>
        current.map((panel) =>
          panel.id !== focusedPanel.id
            ? panel
            : {
                ...panel,
                group,
                // Unpinning keeps what the panel was showing: the letter
                // goes, the symbol on screen does not.
                symbol: group === null ? symbolFor(groups, panel.group, panel.symbol) : panel.symbol,
              },
        ),
      );
    },
    [focusedPanel, groups],
  );

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
      if (result.kind === ParseKind.Empty) return;
      if (result.kind === ParseKind.Error) {
        setWarning(result.message);
        return;
      }
      if (result.kind === ParseKind.Action) {
        if (result.code === GRP_CODE) runGroup(result.tokens);
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
          setGroups((current) => ({ ...current, [group]: result.symbol }));
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
    [here, runHere, runGroup, known, focusedPanel],
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

  useGlobalKeys({ inputRef, paletteOpen: palette.open, openHelp });

  const spec = getPanel(here.code);
  const hasSymbol = here.symbol !== null;
  const panels = listPanels();
  const market_panels = panels.filter((panel) => !panel.needsSymbol);
  const symbol_panels = panels.filter((panel) => panel.needsSymbol);

  return (
    <div className="shell">
      <header className="command-bar">
        <input
          ref={inputRef}
          aria-label="command"
          placeholder="Symbol, function, args… Enter to run"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {warning && <p className="warn">{warning}</p>}
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
        <Workspace panels={dockPanels} onRun={onRun} onActive={setActiveId} groups={groups} />
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
