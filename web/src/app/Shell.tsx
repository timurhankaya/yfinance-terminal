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
  SYMBOL_RE,
} from "../commands/parser";
import { getPanel, isMnemonic, listPanels } from "../commands/registry";
import { Layout } from "../commands/types";
import type { Command, PanelArgs, PanelSpec } from "../commands/types";
import { useGo } from "../commands/go";
import { CommandPalette } from "./CommandPalette";
import { Strip } from "./Strip";
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
  const { symbol: rawSymbol, code: rawCode } = useParams();
  const { search, state } = useLocation();
  const go = useGo();
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
  const spec = getPanel(command.code);
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  const [palette, setPalette] = useState<{ open: boolean; query: string }>({ open: false, query: "" });
  const pendingRef = useRef<{ code: string; args: PanelArgs } | null>(null);

  const submit = useCallback(
    async (text: string) => {
      const result = parse(text, { symbol: command.symbol, code: command.code });
      if (result.kind === ParseKind.Empty) return;
      if (result.kind === ParseKind.Error) {
        setWarning(result.message);
        return;
      }
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
          setWarning("Could not reach the API. Try again.");
          return;
        }
      }
      setWarning(null);
      setDraft("");
      // Hand the keyboard to the panel: while the box keeps focus, j/k/Enter
      // would be typed into it instead of moving a list selection.
      inputRef.current?.blur();
      go(next);
    },
    [command, go],
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
      go({ symbol: text.toUpperCase(), code: pending.code, args: pending.args });
      return;
    }
    void submit(text);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      void submit(draft);
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
    go({ symbol: command.symbol, code: "HELP", args: {} });
  }, [go, command.symbol]);

  useGlobalKeys({ inputRef, paletteOpen: palette.open, openHelp });

  const hasSymbol = command.symbol !== null;
  const panels = listPanels();
  const market_panels = panels.filter((panel) => !panel.needsSymbol);
  const symbol_panels = panels.filter((panel) => panel.needsSymbol);

  let body;
  if (!spec) {
    body = <p className="muted">Unknown function {command.code}.</p>;
  } else if (spec.needsSymbol && !hasSymbol) {
    body = <p className="muted">Type a symbol to begin.</p>;
  } else {
    const Component = spec.component;
    body = <Component symbol={command.symbol} args={command.args} />;
  }

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
          <FnButton key={panel.code} panel={panel} current={command.code} go={go} symbol={command.symbol} runnable />
        ))}
        <span className="fn-group">
          {command.symbol === null ? "This symbol" : command.symbol}
        </span>
        {symbol_panels.map((panel) => (
          <FnButton
            key={panel.code}
            panel={panel}
            current={command.code}
            go={go}
            symbol={command.symbol}
            runnable={hasSymbol}
          />
        ))}
      </nav>
      {hasSymbol && command.symbol !== null && (
        // `headed` is what turns the strip live. A `single` panel gets
        // the symbol and nothing else, so opening a statement or a
        // filing list does not hold a subscription for a price nobody is
        // looking at.
        <Strip symbol={command.symbol} live={spec?.layout === Layout.Headed} />
      )}
      <main className="panel">{body}</main>
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
