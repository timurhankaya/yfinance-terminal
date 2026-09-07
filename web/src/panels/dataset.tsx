// One dataset, loaded and shown. Shared by DS (any dataset by name) and
// the curated tabbed panels (a fixed list of datasets per family).
import { useEffect, useState } from "react";
import type { ReactElement } from "react";
import { ApiError, getCatalog, getDatasetPage, type CatalogEntry, type Row } from "../api/client";
import type { PanelArgs } from "../commands/types";
import { EmptyCard, ErrorCard, usePanelData } from "./common";
import { linksFor } from "./links";
import { DatasetTable } from "./table";

/** How a dataset takes the strip's symbol.
 *  - `auto`: send it when the catalogue says the dataset has a symbol
 *    column and the strip has one; otherwise ask without (the API
 *    refuses with a 422 when the symbol was required, and that shows).
 *  - `none`: never send it: a market-wide view even with a symbol up. */
export type SymbolMode = "auto" | "none";

export interface Loaded {
  entry: CatalogEntry;
  rows: Row[];
  next_cursor: string | null;
  /** The symbol the rows were filtered to, if any. */
  symbol: string | null;
  /** The query the next page continues. */
  params: Record<string, string>;
}

export class UnknownDataset extends Error {
  constructor(readonly name: string) {
    super(`No dataset named ${name}`);
    this.name = "UnknownDataset";
  }
}

export async function loadDataset(
  name: string, symbol: string | null, filters: PanelArgs, mode: SymbolMode,
): Promise<Loaded> {
  const catalog = await getCatalog();
  const entry = catalog.find((e) => e.name === name);
  if (entry === undefined) throw new UnknownDataset(name);
  const params: Record<string, string> = { ...filters };
  const scoped = mode === "auto" && entry.symbol_scoped && symbol !== null;
  if (scoped) params.symbol = symbol;
  try {
    const page = await getDatasetPage(entry.name, params);
    return { entry, rows: page.rows, next_cursor: page.next_cursor, symbol: scoped ? symbol : null, params };
  } catch (err) {
    // The API refuses an unknown filter rather than ignoring it; say
    // which ones this dataset takes so the next attempt is right.
    if (err instanceof ApiError && err.status === 422 && Object.keys(filters).length > 0) {
      const accepted = entry.filters.length > 0 ? entry.filters.map((f) => `${f}=`).join(" ") : "no filters";
      throw new Error(`${err.message}; ${entry.name} accepts ${accepted}`);
    }
    throw err;
  }
}

/** Splits `k=v` tokens into filters. A token without `=` is an error the
 *  panel reports as usage; the panel's own positional tokens are taken
 *  off by the caller first. */
export function parseFilters(tokens: string[], usage: string): PanelArgs {
  const filters: PanelArgs = {};
  for (const token of tokens) {
    const at = token.indexOf("=");
    if (at <= 0) throw new Error(usage);
    filters[token.slice(0, at)] = token.slice(at + 1);
  }
  return filters;
}

/** The args that are filters, i.e. everything except the panel's own keys. */
export function filtersOf(args: PanelArgs, own: string[]): PanelArgs {
  const filters: PanelArgs = {};
  for (const [key, value] of Object.entries(args)) {
    if (!own.includes(key)) filters[key] = value;
  }
  return filters;
}

export function DatasetView(props: {
  name: string;
  symbol: string | null;
  filters: PanelArgs;
  mode: SymbolMode;
}): ReactElement {
  const { name, symbol, filters, mode } = props;
  const filterKey = Object.entries(filters)
    .sort()
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
  const key = `${name}|${symbol ?? ""}|${mode}|${filterKey}`;
  const { state, retry } = usePanelData<Loaded>(
    key,
    () => loadDataset(name, symbol, filters, mode),
    (loaded) => loaded.rows.length === 0,
  );
  // Pages after the first live here; a new query drops them.
  const [extra, setExtra] = useState<{ key: string; rows: Row[]; cursor: string | null; loading: boolean; error: string | null }>({
    key,
    rows: [],
    cursor: null,
    loading: false,
    error: null,
  });
  useEffect(() => {
    setExtra({ key, rows: [], cursor: null, loading: false, error: null });
  }, [key]);

  if (state.kind === "loading") return <p className="muted">Loading {name}…</p>;
  if (state.kind === "missing") return <p className="card card-error">No dataset named {name}.</p>;
  if (state.kind === "error") {
    return <ErrorCard message={describeError(state.message, filters)} onRetry={retry} />;
  }
  if (state.kind === "empty") return <EmptyCard what={`${name} rows`} />;
  const { entry } = state.data;
  const current = extra.key === key ? extra : { key, rows: [], cursor: null, loading: false, error: null };
  const rows = [...state.data.rows, ...current.rows];
  const cursor = current.rows.length > 0 || current.cursor !== null ? current.cursor : state.data.next_cursor;
  const loadMore = async () => {
    if (!cursor || current.loading) return;
    setExtra({ ...current, loading: true, error: null });
    try {
      const page = await getDatasetPage(entry.name, state.data.params, cursor);
      setExtra((e) =>
        e.key === key ? { ...e, rows: [...e.rows, ...page.rows], cursor: page.next_cursor, loading: false } : e,
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setExtra((e) => (e.key === key ? { ...e, loading: false, error: message } : e));
    }
  };
  return (
    <section>
      <p className="detail-meta">
        <strong>{entry.name}</strong> · {entry.family} · {entry.description}
        {entry.filters.length > 0 && <> · filters: {entry.filters.map((f) => `${f}=`).join(" ")}</>}
        {state.data.symbol !== null && <> · {state.data.symbol}</>}
      </p>
      <DatasetTable
        key={entry.name}
        columns={entry.columns}
        rows={rows}
        hide={state.data.symbol !== null ? ["symbol"] : []}
        links={linksFor(entry.name, entry.columns.map((c) => c.name))}
        onLoadMore={cursor ? () => void loadMore() : undefined}
        loadingMore={current.loading}
      />
      {current.error && <p className="card card-error">Could not load the next page: {current.error}</p>}
    </section>
  );
}

function describeError(message: string, filters: PanelArgs): string {
  const keys = Object.keys(filters);
  return keys.length > 0 ? `${message} (filters: ${keys.join(", ")})` : message;
}

/** Whether an error is the API refusing a symbol-less request for a
 *  symbol-scoped dataset, so the panel can say "type a symbol" instead. */
export function needsSymbol(err: unknown): boolean {
  return err instanceof ApiError && err.status === 422;
}
