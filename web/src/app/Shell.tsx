import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ApiError, getSymbol, UnauthorizedError } from "../api/client";
import { commandToPath, parse, pathToCommand, SYMBOL_RE } from "../commands/parser";
import { getPanel, isMnemonic, listPanels } from "../commands/registry";
import type { PanelArgs } from "../commands/types";
import { CommandPalette } from "./CommandPalette";
import { useGlobalKeys } from "./keys";
import { useSession } from "./session";

export const LAST_KEY = "yfin.ui.last";

export function Shell() {
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
    try {
      localStorage.setItem(LAST_KEY, `/ui/t/${rawSymbol}/${rawCode}`);
    } catch {
      // Private mode or blocked storage: the redirect just loses its hint.
    }
  }, [rawSymbol, rawCode]);

  const submit = useCallback(
    async (text: string) => {
      const result = parse(text, { symbol: command.symbol, code: command.code });
      if (result.kind === "empty") return;
      if (result.kind === "error") {
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
          if (err instanceof UnauthorizedError) {
            pendingDraftRef.current = text;
            requireLogin();
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
    [command, navigate, requireLogin],
  );

  // Focus the box once the session exists. A static autoFocus would race
  // the login modal's password field on first load and win.
  useEffect(() => {
    if (me?.authenticated) inputRef.current?.focus();
  }, [me?.authenticated]);

  // The last command re-runs after a successful login.
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
    if (event.key === "Escape") {
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
      {hasSymbol && (
        <div className="strip">
          <span className="strip-symbol">{command.symbol}</span>
        </div>
      )}
      <main className="panel">{body}</main>
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
