import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ApiError, getSymbol } from "../api/client";
import { DEFAULT_CODE, NO_SYMBOL, commandToPath, parse, ParseKind, pathToCommand, SYMBOL_RE } from "../commands/parser";
import { getPanel, isMnemonic, listPanels } from "../commands/registry";
import { Layout } from "../commands/types";
import type { PanelArgs } from "../commands/types";
import { CommandPalette } from "./CommandPalette";
import { Strip } from "./Strip";
import { useGlobalKeys } from "./keys";

export const LAST_KEY = "yfin.ui.last";

export function Shell() {
  const { symbol: rawSymbol = NO_SYMBOL, code: rawCode = DEFAULT_CODE } = useParams();
  const { search } = useLocation();
  const navigate = useNavigate();
  // The URL is the one input a panel gets that `parseArgs` never saw, so
  // the panel's own rules are applied to it here -- once, for every
  // panel, rather than in each panel that remembered to.
  const command = useMemo(() => {
    const fromPath = pathToCommand(rawSymbol, rawCode, search);
    const normalize = getPanel(fromPath.code)?.normalizeArgs;
    return normalize === undefined ? fromPath : { ...fromPath, args: normalize(fromPath.args) };
  }, [rawSymbol, rawCode, search]);
  const spec = getPanel(command.code);
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  const [palette, setPalette] = useState<{ open: boolean; query: string }>({ open: false, query: "" });
  const pendingRef = useRef<{ code: string; args: PanelArgs } | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(LAST_KEY, `/ui/t/${rawSymbol}/${rawCode}`);
    } catch {
      // Private mode or blocked storage: the redirect just loses its hint.
    }
  }, [rawSymbol, rawCode]);

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
      void navigate(commandToPath(next));
    },
    [command, navigate],
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
      void navigate(commandToPath({ symbol: text.toUpperCase(), code: pending.code, args: pending.args }));
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
    void navigate(commandToPath({ symbol: command.symbol, code: "HELP", args: {} }));
  }, [navigate, command.symbol]);

  useGlobalKeys({ inputRef, paletteOpen: palette.open, openHelp });

  const hasSymbol = command.symbol !== null;

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
      <nav className="fnbar" aria-label="functions">
        {listPanels().map((panel) => {
          const runnable = !panel.needsSymbol || hasSymbol;
          return (
            <button
              key={panel.code}
              type="button"
              className={panel.code === command.code ? "fn fn-active" : "fn"}
              aria-current={panel.code === command.code ? "page" : undefined}
              disabled={!runnable}
              title={panel.title}
              onClick={() => void navigate(commandToPath({ symbol: command.symbol, code: panel.code, args: {} }))}
            >
              {panel.code}
            </button>
          );
        })}
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
