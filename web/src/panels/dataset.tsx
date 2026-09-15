// One dataset, loaded and shown. Shared by DS (any dataset by name) and
// the curated tabbed panels (a fixed list of datasets per family).
import { useMemo } from "react";
import type { ReactElement } from "react";
import {
  ApiError,
  getCatalog,
  getDatasetPage,
  PAGE_LIMIT,
  PAGE_SIZE,
  WireType,
  type CatalogEntry,
  type Row,
} from "../api/client";
import { DatasetFilters, NumberArg } from "./controls";
import type { PanelArgs } from "../commands/types";
import { EmptyState, ErrorCard, LoadState, NextPageError, usePagedRows, usePanelData } from "./common";
import { gridNames } from "./grid";
import { linksFor } from "./links";
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
  name: string, symbol: string | null, filters: PanelArgs, mode: SymbolMode, limit: number,
): Promise<DatasetPage> {
  const catalog = await getCatalog();
  const entry = catalog.find((e) => e.name === name);
  if (entry === undefined) throw new Error(`No dataset named ${name}`);
  const params: Record<string, string> = { ...filters };
  const scoped = mode === SymbolMode.Auto && entry.symbol_scoped && symbol !== null;
  if (scoped) params.symbol = symbol;
  try {
    const page = await getDatasetPage(entry.name, params, null, limit);
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
  chart?: TabChart;
  /** Rows per request. The panel keeps it in its own args, so a page of
   *  500 is part of the address like everything else. */
  pageSize?: number;
  /** Changes an argument -- a filter, or the page size. Absent leaves the
   *  filters as the sentence they were: a panel that does not own its
   *  args cannot offer to edit them. */
  onArgs?: (next: PanelArgs) => void;
}): ReactElement {
  const { name, symbol, filters, mode, chart, pageSize = PAGE_SIZE, onArgs } = props;
  const filterKey = Object.entries(filters)
    .sort()
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
  const key = `${name}|${symbol ?? ""}|${mode}|${filterKey}|${pageSize}`;
  const { state: catalogState } = usePanelData(name, async () => {
    const catalog = await getCatalog();
    return catalog.find((entry) => entry.name === name) ?? null;
  });
  const { state, retry } = usePanelData<DatasetPage>(
    key,
    () => loadDataset(name, symbol, filters, mode, pageSize),
  );
  const first = state.kind === LoadState.Ready ? state.data : null;
  const entry = first?.entry ?? (catalogState.kind === LoadState.Ready ? catalogState.data : null);
  const paged = usePagedRows<Row>(key, first?.next_cursor ?? null, (cursor) =>
    first === null
      ? Promise.reject(new Error("no first page"))
      : getDatasetPage(first.entry.name, first.params, cursor, pageSize),
  );
  // Hoisted above the early returns, because the sparkline hook below
  // needs the rows and a hook cannot run after a conditional return.
  const rows = useMemo(
    () => (first === null ? EMPTY_ROWS : [...first.rows, ...paged.rows]),
    [first, paged.rows],
  );
  const pageControl = onArgs === undefined ? undefined : (
    <NumberArg label="Rows per load" value={pageSize} min={1} max={PAGE_LIMIT} onSet={(value) => onArgs({ rows: String(value) })} />
  );
  return (
    <section className="dataset-view" aria-busy={state.kind === LoadState.Loading}>
      <div className="dataset-heading">
        <strong>{name.replaceAll("_", " ")}</strong>
        {entry && <span className="muted">{entry.description}</span>}
        {entry?.symbol_scoped && mode === SymbolMode.Auto && symbol && <span className="dataset-scope">{symbol}</span>}
      </div>
      {onArgs !== undefined && entry && <DatasetFilters
        key={`${name}|${filterKey}`}
        fields={entry.filters.map((filter) => ({ name: filter, type: isDateFilter(entry, filter) ? "date" : "text" }))}
        values={filters}
        onApply={onArgs}
      />}
      {state.kind === LoadState.Loading && <p className="muted" role="status">Loading {name}…</p>}
      {state.kind === LoadState.Missing && <p className="card card-error">No dataset named {name}.</p>}
      {state.kind === LoadState.Error && <ErrorCard message={describeError(state.message, filters)} onRetry={retry} />}
      {first && <>
        {chart !== undefined && rows.length > 0 && <TabChartView kind={chart} rows={rows} symbol={first.symbol ?? symbol} />}
        {rows.length === 0 && paged.cursor === null ? <>
          <EmptyState
            title={Object.keys(filters).length > 0 ? "No records for these filters." : "No records available"}
            description={`No ${name.replaceAll("_", " ")} records are available${first.symbol ? ` for ${first.symbol}` : ""}${Object.keys(filters).length > 0 ? " with the selected filters" : ""}. Try another tab or symbol, adjust any filters, or retry.`}
            onRetry={retry}
          />
          {pageControl && <div className="empty-page-control">{pageControl}</div>}
        </> : <DatasetTable
          key={`${name}|${filterKey}|${symbol ?? ""}`}
          columns={first.entry.columns}
          rows={rows}
          grid={gridNames(first.entry.name, first.entry.columns, first.symbol !== null ? ["symbol"] : [])}
          links={linksFor(first.entry.name, first.entry.columns.map((c) => c.name))}
          onLoadMore={paged.cursor === null ? undefined : paged.loadMore}
          loadingMore={paged.loadingMore}
          pageControl={pageControl}
        />}
        {paged.error !== null && <NextPageError message={paged.error} />}
      </>}
    </section>
  );
}

/** Whether a filter names a column the catalogue calls a date.
 *
 *  From the schema, not from the filter's spelling: `as_of_date` and
 *  `session_date` are dates and `screen_key` is not, and only the
 *  catalogue knows which is which. */
function isDateFilter(entry: CatalogEntry, filter: string): boolean {
  const column = entry.columns.find((c) => c.name === filter);
  return column !== undefined && (column.type === WireType.Date || column.type === WireType.DateTime);
}

//: Shared empties, so a re-render with nothing loaded does not restart
//: the sparkline query with a new array.
const EMPTY_ROWS: Row[] = [];

function describeError(message: string, filters: PanelArgs): string {
  const keys = Object.keys(filters);
  return keys.length > 0 ? `${message} (filters: ${keys.join(", ")})` : message;
}
