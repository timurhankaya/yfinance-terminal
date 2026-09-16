import { Command } from "cmdk";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, MouseEvent } from "react";
import { searchSymbols, SEARCH_MIN_PREFIX, type SymbolSummary } from "../api/client";
import { listPanels } from "../commands/registry";

export interface CommandPaletteProps {
  open: boolean;
  canSplit?: boolean;
  query: string;
  onQuery: (query: string) => void;
  /** `split` opens the pick BESIDE the panel instead of in it, which is
   *  the same gesture the command box takes (Ctrl/Cmd+Enter). */
  onPick: (text: string, split: boolean, kind: "symbol" | "function") => void;
  onClose: () => void;
}

const DEBOUNCE_MS = 150;

/** The `?`/`Ctrl+K` picker: registered mnemonics plus, once the query is at
 *  least SEARCH_MIN_PREFIX characters, matching symbols from the API. Uses
 *  the plain `Command` from cmdk, never `Command.Dialog` — its Radix dialog
 *  injects a `<style>` element the page's CSP blocks. */
export function CommandPalette({ open, query, onQuery, onPick, onClose, canSplit = true }: CommandPaletteProps) {
  const [lookup, setLookup] = useState<{ query: string; status: "loading" | "ready" | "error"; rows: SymbolSummary[] }>({ query: "", status: "ready", rows: [] });
  const [attempt, setAttempt] = useState(0);
  const host = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const term = query.trim();
  const eligible = term.length >= SEARCH_MIN_PREFIX;
  const loading = eligible && (lookup.query !== term || lookup.status === "loading");
  const failed = eligible && lookup.query === term && lookup.status === "error";
  const results = lookup.query === term && lookup.status === "ready" ? lookup.rows : [];
  // Read at select time, written in the CAPTURE phase: cmdk owns the
  // Enter key and its `onSelect` carries no event, so the modifier has
  // to be recorded before the event reaches cmdk's own handler.
  const split = useRef(false);

  useEffect(() => {
    if (!open || !eligible) return;
    let cancelled = false;
    setLookup({ query: term, status: "loading", rows: [] });
    const timer = setTimeout(() => {
      void searchSymbols(term).then((rows) => {
        if (!cancelled) setLookup({ query: term, status: "ready", rows });
      }).catch(() => {
        if (!cancelled) setLookup({ query: term, status: "error", rows: [] });
      });
    }, DEBOUNCE_MS);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [open, term, eligible, attempt]);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement;
    const backdrop = host.current?.parentElement;
    const siblings = [...(backdrop?.parentElement?.children ?? [])]
      .filter((element): element is HTMLElement => element instanceof HTMLElement && element !== backdrop)
      .map((element) => ({ element, inert: element.inert }));
    for (const { element } of siblings) element.inert = true;
    input.current?.focus();
    return () => {
      for (const { element, inert } of siblings) element.inert = inert;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, [open]);

  if (!open) return null;

  const q = query.trim().toUpperCase();
  const panels = listPanels().filter(
    (p) => p.code !== "PG" && (q === "" || p.code.toUpperCase().startsWith(q) || p.title.toUpperCase().includes(q)),
  );

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === "Tab") {
      const controls = host.current?.querySelectorAll<HTMLElement>('input, button:not(:disabled), [tabindex="0"]');
      if (!controls?.length) return;
      const first = controls[0]!;
      const last = controls[controls.length - 1]!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }

  function armSplit(event: KeyboardEvent | MouseEvent) {
    split.current = event.ctrlKey || event.metaKey;
  }

  function pick(text: string, kind: "symbol" | "function") {
    const beside = canSplit && split.current;
    split.current = false;
    onPick(text, beside, kind);
  }

  return (
    <div className="palette-backdrop" onClick={onClose}>
      <div
        className="palette"
        ref={host}
        role="dialog"
        aria-modal="true"
        aria-labelledby="search-heading"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
        onKeyDownCapture={armSplit}
        onClickCapture={armSplit}
      >
        <div className="palette-head">
          <div><span className="palette-eyebrow">YFIN / DISCOVERY</span><h2 id="search-heading">Search the terminal</h2></div>
          <button type="button" className="palette-close" onClick={onClose} aria-label="Close search">Esc ×</button>
        </div>
        <Command shouldFilter={false} label="Command palette">
          <Command.Input ref={input} aria-label="palette" placeholder="Search a ticker, company or function…" value={query} onValueChange={onQuery} />
          <Command.List aria-busy={loading}>
            {!eligible && <p className="palette-state">Type a ticker or company name to find a symbol. Browse functions below.</p>}
            {loading && <p className="palette-state" role="status">Searching symbols…</p>}
            {failed && <div className="palette-state palette-error" role="alert">Symbol search is unavailable. Try again.<button type="button" className="chip" onClick={() => setAttempt((n) => n + 1)}>Retry search</button></div>}
            {eligible && !loading && !failed && results.length === 0 && <p className="palette-state" role="status">No symbols found for “{term}”. Try a ticker or company name.</p>}
            {panels.length > 0 && (
              <Command.Group heading="Functions">
                {panels.map((p) => (
                  <Command.Item key={p.code} value={`function:${p.code}`} aria-label={`${p.code} — ${p.title}`} onSelect={() => pick(p.code, "function")}>
                    <span className="palette-code">{p.code}</span><span className="palette-name">{p.title}</span><span className="palette-badge">{p.needsSymbol ? "Symbol" : "Market"}</span>
                  </Command.Item>
                ))}
              </Command.Group>
            )}
            {eligible && results.length > 0 && (
              <Command.Group heading="Symbols">
                {results.map((r) => (
                  <Command.Item key={r.symbol} value={`symbol:${r.symbol}`} aria-label={`${r.symbol} — ${r.long_name ?? r.short_name ?? r.symbol}${r.exchange ? ` · ${r.exchange}` : ""}`} onSelect={() => pick(r.symbol, "symbol")}>
                    <span className="palette-code">{r.symbol}</span>
                    <span className="palette-name">{r.long_name ?? r.short_name ?? "Name unavailable"}{r.quote_type !== null && r.quote_type !== undefined && <span className="palette-type">{r.quote_type}</span>}</span>
                    <span className="palette-badge">{r.exchange ?? "—"}</span>
                  </Command.Item>
                ))}
              </Command.Group>
            )}
          </Command.List>
          <p className="palette-note"><span className="palette-keys">↑ ↓ Navigate · Enter Open · Esc Close</span>
            Search matches the ticker or the company name. Enter opens the pick here;{" "}
            {canSplit ? <><code className="usage">Ctrl+Enter</code> opens it in a panel beside this one.</> : <>use <code className="usage">+</code> on the page to open a workspace.</>}
          </p>
        </Command>
      </div>
    </div>
  );
}
