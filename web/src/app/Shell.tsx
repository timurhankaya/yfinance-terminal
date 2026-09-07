import { useEffect, useState } from "react";
import type { KeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router";
import { DES } from "../panels/DES";

export const LAST_KEY = "yfin.ui.last";

export function Shell() {
  const { symbol = "-", code = "DES" } = useParams();
  const navigate = useNavigate();
  const [draft, setDraft] = useState("");

  useEffect(() => {
    try {
      localStorage.setItem(LAST_KEY, `/ui/t/${symbol}/${code}`);
    } catch {
      // Private mode or blocked storage: the redirect just loses its hint.
    }
  }, [symbol, code]);

  // 1a: a plain symbol box. 1b replaces this with the command parser.
  function onKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    const next = draft.trim().toUpperCase();
    if (next === "") return;
    setDraft("");
    void navigate(`/ui/t/${encodeURIComponent(next)}/DES`);
  }

  const hasSymbol = symbol !== "-";
  return (
    <div className="shell">
      <header className="command-bar">
        <input
          aria-label="command"
          placeholder="Symbol, then Enter"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          autoFocus
        />
      </header>
      {hasSymbol && (
        <div className="strip">
          <span className="strip-symbol">{symbol}</span>
        </div>
      )}
      <main className="panel">
        {code === "DES" ? (
          hasSymbol ? <DES symbol={symbol} /> : <p className="muted">Type a symbol to begin.</p>
        ) : (
          <p className="muted">Unknown function {code}.</p>
        )}
      </main>
    </div>
  );
}
