// One dataset, loaded and shown. Shared by DS (any dataset by name) and
// the curated tabbed panels (a fixed list of datasets per family).
import { useMemo } from "react";
import type { ReactElement } from "react";
import { ApiError, getCatalog, getDatasetPage, type CatalogEntry, type Row } from "../api/client";
import type { PanelArgs } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, NextPageError, usePagedRows, usePanelData, type Column } from "./common";
import { linksFor } from "./links";
import { SparkCell, rowSymbols, sparkLabel, useSparklines } from "./spark";
import { TabChartView, type TabChart } from "./tabcharts";
import { DatasetTable } from "./table";

/** How a dataset takes the strip's symbol.
 *  - `auto`: send it when the catalogue says the dataset has a symbol
 *    column and the strip has one; otherwise ask without (the API
 *    refuses with a 422 when the symbol was required, and that shows).
 *  - `none`: never send it: a market-wide view even with a symbol up. */
export enum SymbolMode {
  Auto = "auto",
  None = "none",
}

/** A column a dataset view draws beside the catalogue's own.
 *
 *  Declared, not built: a `Tab` is configuration (`curated.tsx:1-3`) and
 *  cannot fetch, while a sparkline column needs the symbols of the rows
 *  that loaded. The view turns the declaration into a `Column<Row>` once
 *  it has them. */
export enum ExtraColumn {
  Sparkline = "sparkline",
}

//: Shared empty, so a view without extras does not hand the sparkline
//: hook a new array on every render.
const NO_EXTRAS: ExtraColumn[] = [];

export interface DatasetPage {
  entry: CatalogEntry;
  rows: Row[];
  next_cursor: string | null;
  /** The symbol the rows were filtered to, if any. */
  symbol: string | null;
  /** The query the next page continues. */
  params: Record<string, string>;
}

async function loadDataset(
  name: string, symbol: string | null, filters: PanelArgs, mode: SymbolMode,
): Promise<DatasetPage> {
  const catalog = await getCatalog();
  const entry = catalog.find((e) => e.name === name);
  if (entry === undefined) throw new Error(`No dataset named ${name}`);
  const params: Record<string, string> = { ...filters };
  const scoped = mode === SymbolMode.Auto && entry.symbol_scoped && symbol !== null;
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
  extra?: ExtraColumn[];
  chart?: TabChart;
}): ReactElement {
  const { name, symbol, filters, mode, extra = NO_EXTRAS, chart } = props;
  const filterKey = Object.entries(filters)
    .sort()
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
  const key = `${name}|${symbol ?? ""}|${mode}|${filterKey}`;
  const { state, retry } = usePanelData<DatasetPage>(
    key,
    () => loadDataset(name, symbol, filters, mode),
    (loaded) => loaded.rows.length === 0,
  );
  const first = state.kind === LoadState.Ready ? state.data : null;
  const paged = usePagedRows<Row>(key, first?.next_cursor ?? null, (cursor) =>
    first === null
      ? Promise.reject(new Error("no first page"))
      : getDatasetPage(first.entry.name, first.params, cursor),
  );
  // Hoisted above the early returns, because the sparkline hook below
  // needs the rows and a hook cannot run after a conditional return.
  const rows = useMemo(
    () => (first === null ? EMPTY_ROWS : [...first.rows, ...paged.rows]),
    [first, paged.rows],
  );
  const wantsSpark = extra.includes(ExtraColumn.Sparkline);
  const sparkSymbols = useMemo(
    () => (wantsSpark ? rowSymbols(rows) : EMPTY_SYMBOLS),
    [wantsSpark, rows],
  );
  const spark = useSparklines(sparkSymbols);
  const extraColumns = useMemo<Column<Row>[]>(
    () =>
      wantsSpark
        ? [
            {
              key: "__spark",
              label: sparkLabel(spark),
              format: (row) =>
                typeof row.symbol === "string" ? (
                  <SparkCell data={spark} symbol={row.symbol} />
                ) : (
                  "—"
                ),
            },
          ]
        : [],
    [wantsSpark, spark],
  );

  if (state.kind === LoadState.Loading) return <p className="muted">Loading {name}…</p>;
  if (state.kind === LoadState.Missing) return <p className="card card-error">No dataset named {name}.</p>;
  if (state.kind === LoadState.Error) {
    return <ErrorCard message={describeError(state.message, filters)} onRetry={retry} />;
  }
  if (state.kind === LoadState.Empty) return <EmptyCard what={`${name} rows`} />;
  const { entry } = state.data;
  return (
    <section>
      <p className="detail-meta">
        <strong>{entry.name}</strong> · {entry.family} · {entry.description}
        {entry.filters.length > 0 && <> · filters: {entry.filters.map((f) => `${f}=`).join(" ")}</>}
        {state.data.symbol !== null && <> · {state.data.symbol}</>}
      </p>
      {/* Above the table, never instead of it: the one thing a chart
          cannot show is the exact figure. */}
      {chart !== undefined && <TabChartView kind={chart} rows={rows} symbol={state.data.symbol ?? symbol} />}
      <DatasetTable
        key={entry.name}
        columns={entry.columns}
        rows={rows}
        hide={state.data.symbol !== null ? ["symbol"] : []}
        links={linksFor(entry.name, entry.columns.map((c) => c.name))}
        onLoadMore={paged.cursor === null ? undefined : paged.loadMore}
        loadingMore={paged.loadingMore}
        extra={extraColumns}
      />
      {paged.error !== null && <NextPageError message={paged.error} />}
    </section>
  );
}

//: Shared empties, so a re-render with nothing loaded does not restart
//: the sparkline query with a new array.
const EMPTY_ROWS: Row[] = [];
const EMPTY_SYMBOLS: string[] = [];

function describeError(message: string, filters: PanelArgs): string {
  const keys = Object.keys(filters);
  return keys.length > 0 ? `${message} (filters: ${keys.join(", ")})` : message;
}
