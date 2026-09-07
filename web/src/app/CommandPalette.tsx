import { Command } from "cmdk";
import { useEffect, useState } from "react";
import type { KeyboardEvent } from "react";
import { searchSymbols, SEARCH_MIN_PREFIX, type SymbolSummary } from "../api/client";
import { listPanels } from "../commands/registry";

export interface CommandPaletteProps {
  open: boolean;
  query: string;
  onQuery: (query: string) => void;
  onPick: (text: string) => void;
  onClose: () => void;
}

const DEBOUNCE_MS = 150;

/** The `?`/`Ctrl+K` picker: registered mnemonics plus, once the query is at
 *  least SEARCH_MIN_PREFIX characters, matching symbols from the API. Uses
 *  the plain `Command` from cmdk, never `Command.Dialog` — its Radix dialog
 *  injects a `<style>` element the page's CSP blocks. */
export function CommandPalette({ open, query, onQuery, onPick, onClose }: CommandPaletteProps) {
  const [results, setResults] = useState<SymbolSummary[]>([]);

  useEffect(() => {
    if (!open || query.length < SEARCH_MIN_PREFIX) {
      setResults([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      void searchSymbols(query)
        .then((rows) => {
          if (!cancelled) setResults(rows);
        })
        .catch(() => {
          if (!cancelled) setResults([]);
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [open, query]);

  if (!open) return null;

  const q = query.trim().toUpperCase();
  const panels = listPanels().filter(
    (p) => q === "" || p.code.toUpperCase().startsWith(q) || p.title.toUpperCase().includes(q),
  );

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
  }

  return (
    <div className="palette-backdrop" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
        <Command shouldFilter={false} label="Command palette">
          <Command.Input aria-label="palette" autoFocus value={query} onValueChange={onQuery} />
          <Command.List>
            {panels.length > 0 && (
              <Command.Group heading="Functions">
                {panels.map((p) => (
                  <Command.Item key={p.code} value={p.code} onSelect={() => onPick(p.code)}>
                    {p.code} — {p.title}
                  </Command.Item>
                ))}
              </Command.Group>
            )}
            {query.length >= SEARCH_MIN_PREFIX && results.length > 0 && (
              <Command.Group heading="Symbols">
                {results.map((r) => (
                  <Command.Item key={r.symbol} value={r.symbol} onSelect={() => onPick(r.symbol)}>
                    {r.symbol} — {r.long_name ?? r.short_name}
                  </Command.Item>
                ))}
              </Command.Group>
            )}
          </Command.List>
          <p className="palette-note">Symbol search matches the start of the ticker only, not company names.</p>
        </Command>
      </div>
    </div>
  );
}
