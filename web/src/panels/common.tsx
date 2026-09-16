import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { ApiError } from "../api/client";
import { LOCALE } from "./format";
import { usePanelFocus } from "../workspace/frame";

/** Where a panel's one load has got to. */
export enum LoadState {
  Loading = "loading",
  Ready = "ready",
  Empty = "empty",
  Missing = "missing",
  Error = "error",
}

export type Loaded<T> =
  | { kind: LoadState.Loading }
  | { kind: LoadState.Ready; data: T }
  | { kind: LoadState.Empty }
  | { kind: LoadState.Missing }
  | { kind: LoadState.Error; message: string };

/** Loads on mount and re-runs whenever `key` changes; drops stale responses;
 *  404 -> missing; any other failure -> error; isEmpty(data) -> empty. */
export function usePanelData<T>(
  key: string,
  load: () => Promise<T>,
  isEmpty?: (data: T) => boolean,
): { state: Loaded<T>; retry: () => void } {
  const [state, setState] = useState<Loaded<T>>({ kind: LoadState.Loading });
  // Refs, not deps: panels pass inline lambdas, and a new function each
  // render must not restart the load.
  const loadRef = useRef(load);
  loadRef.current = load;
  const isEmptyRef = useRef(isEmpty);
  isEmptyRef.current = isEmpty;
  const tokenRef = useRef({ cancelled: false });

  const run = useCallback(async () => {
    tokenRef.current.cancelled = true;
    const token = { cancelled: false };
    tokenRef.current = token;
    setState({ kind: LoadState.Loading });
    try {
      const data = await loadRef.current();
      if (token.cancelled) return;
      const empty = isEmptyRef.current;
      setState(empty && empty(data) ? { kind: LoadState.Empty } : { kind: LoadState.Ready, data });
    } catch (err) {
      if (token.cancelled) return;
      if (err instanceof ApiError && err.status === 404) setState({ kind: LoadState.Missing });
      else setState({ kind: LoadState.Error, message: err instanceof Error ? err.message : String(err) });
    }
  }, []);

  useEffect(() => {
    void run();
    return () => {
      tokenRef.current.cancelled = true;
    };
  }, [key, run]);

  const retry = useCallback(() => {
    void run();
  }, [run]);

  return { state, retry };
}

/** The newest payload of `key`, kept while a RELOAD of the same query is
 *  in flight: `usePanelData` goes back to `Loading` on every re-run, which
 *  would unmount a chart's pan and zoom or a list's selection. `key` is
 *  the query's IDENTITY, not the reload counter, so a different query
 *  still drops the old payload. */
export function useKeptData<T>(key: string, state: Loaded<T>): T | null {
  const [kept, setKept] = useState<{ key: string; data: T } | null>(null);
  useEffect(() => {
    if (state.kind === LoadState.Ready) setKept({ key, data: state.data });
  }, [key, state]);
  if (state.kind === LoadState.Ready) return state.data;
  return kept !== null && kept.key === key ? kept.data : null;
}

export interface PagedRows<T> {
  /** The pages after the first, in the order they were read. */
  rows: T[];
  /** The cursor the next page continues, or null when there is none. */
  cursor: string | null;
  loadMore: () => void;
  loadingMore: boolean;
  /** Why the last "Load more" failed, or null. Never silence: a button
   *  that does nothing is indistinguishable from a page with no rows. */
  error: string | null;
}

interface Paged<T> {
  key: string;
  fetched: boolean;
  rows: T[];
  cursor: string | null;
  loading: boolean;
  error: string | null;
}

/** Cursor paging on top of a first page some other loader fetched: the
 *  reset on a new query, the in-flight guard and the error are the same
 *  for every list. `key` identifies the query; a new one drops the pages. */
export function usePagedRows<T>(
  key: string,
  firstCursor: string | null,
  fetchPage: (cursor: string) => Promise<{ rows: T[]; next_cursor: string | null }>,
): PagedRows<T> {
  const [paged, setPaged] = useState<Paged<T>>({ key, fetched: false, rows: [], cursor: null, loading: false, error: null });
  // A ref, not a dep: callers pass an inline lambda, and a new function
  // every render must not reset the pages.
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;

  const request = useRef<{ busy: boolean; cancelled: boolean }>({ busy: false, cancelled: false });
  useEffect(() => {
    request.current.cancelled = true;
    request.current = { busy: false, cancelled: false };
    setPaged({ key, fetched: false, rows: [], cursor: null, loading: false, error: null });
    return () => { request.current.cancelled = true; };
  }, [key]);

  // The state can be one render behind the key; read it as empty until
  // the effect above catches up. Memoised so `loadMore` below keeps its
  // identity across renders that changed nothing.
  const current = useMemo<Paged<T>>(
    () => (paged.key === key ? paged : { key, fetched: false, rows: [], cursor: null, loading: false, error: null }),
    [paged, key],
  );
  // Once a page has been read, its `next_cursor` is the truth -- null
  // included, which is what "no more pages" looks like.
  const cursor = current.fetched ? current.cursor : firstCursor;

  const loadMore = useCallback(() => {
    if (cursor === null || current.loading || request.current.busy) return;
    const token = request.current;
    token.busy = true;
    setPaged({ ...current, loading: true, error: null });
    void (async () => {
      try {
        const page = await fetchRef.current(cursor);
        if (token.cancelled) return;
        setPaged((p) =>
          p.key === key
            ? { ...p, fetched: true, rows: [...p.rows, ...page.rows], cursor: page.next_cursor, loading: false }
            : p,
        );
      } catch (err) {
        if (token.cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setPaged((p) => (p.key === key ? { ...p, loading: false, error: message } : p));
      } finally {
        token.busy = false;
      }
    })();
  }, [key, cursor, current]);

  return { rows: current.rows, cursor, loadMore, loadingMore: current.loading, error: current.error };
}

export function NextPageError(props: { message: string }): ReactElement {
  return <p className="card card-error">Could not load the next page: {props.message}</p>;
}

export function ErrorCard(props: { message: string; onRetry: () => void }): ReactElement {
  return (
    <p className="card card-error">
      Could not load: {props.message} <button onClick={props.onRetry}>Retry</button>
    </p>
  );
}

export function EmptyState(props: { title: string; description: string; onRetry?: () => void }): ReactElement {
  return <section className="empty-state" role="status">
    <span className="empty-state-icon" aria-hidden="true">—</span>
    <h3>{props.title}</h3>
    <p>{props.description}</p>
    {props.onRetry && <button type="button" onClick={props.onRetry}>Retry</button>}
  </section>;
}

export function EmptyCard(props: { what: string }): ReactElement {
  return <EmptyState title={`No ${props.what} for this symbol.`} description="There are no records available for this view. Try another tab, period or symbol." />;
}

export function MissingCard(props: { symbol: string | null }): ReactElement {
  return <p className="card card-error">No such symbol: {props.symbol}</p>;
}

export interface Column<Row> {
  key: string;
  label: string;
  align?: "left" | "right";
  format?: (row: Row) => ReactNode;
  /** Off for a column with no value to compare -- a sparkline is a
   *  shape, not a number. Defaults to on. */
  sortable?: boolean;
}

export enum SortDirection {
  Asc = "asc",
  Desc = "desc",
}

export interface Sort {
  key: string;
  direction: SortDirection;
}

//: What a cell is worth, for ordering. The archive sends decimals as
//: strings, so "9" must not sort after "10"; a date must order by time
//: rather than by its first character; and anything left is compared as
//: text in the terminal's own fixed locale.
type Ranked = { number: number } | { text: string };

function rank(value: unknown): Ranked | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "boolean") return { number: value ? 1 : 0 };
  if (typeof value === "number") return Number.isFinite(value) ? { number: value } : null;
  if (typeof value !== "string") return { text: String(value) };
  const numeric = Number(value);
  if (value.trim() !== "" && Number.isFinite(numeric)) return { number: numeric };
  const time = Date.parse(value);
  if (!Number.isNaN(time) && /\d{4}-\d{2}-\d{2}/.test(value)) return { number: time };
  return { text: value };
}

function compare(left: Ranked, right: Ranked): number {
  if ("number" in left && "number" in right) return left.number - right.number;
  const text = "text" in left ? left.text : String(left.number);
  const other = "text" in right ? right.text : String(right.number);
  return text.localeCompare(other, LOCALE);
}

/** Rows in the order the reader asked for, and the control that asks.
 *  The sort lives with whoever owns j/k: sorting inside the table would
 *  leave the keyboard walking the old order under the new one. */
export function useSortedRows<Row extends Record<string, unknown>>(
  rows: Row[],
): { rows: Row[]; sort: Sort | null; toggle: (key: string) => void } {
  const [sort, setSort] = useState<Sort | null>(null);

  const toggle = useCallback((key: string) => {
    setSort((current) => {
      if (current === null || current.key !== key) return { key, direction: SortDirection.Asc };
      if (current.direction === SortDirection.Asc) return { key, direction: SortDirection.Desc };
      // Third press puts the table back the way the panel sent it --
      // which for most of them is the order the archive is keyed in.
      return null;
    });
  }, []);

  const sorted = useMemo(() => {
    if (sort === null) return rows;
    const sign = sort.direction === SortDirection.Asc ? 1 : -1;
    return [...rows].sort((a, b) => {
      const left = rank(a[sort.key]);
      const right = rank(b[sort.key]);
      // An empty cell sorts last whichever way the column is pointed: an
      // absence is not the smallest number, so the direction does not
      // apply to it.
      if (left === null || right === null) return (left === null ? 1 : 0) - (right === null ? 1 : 0);
      return sign * compare(left, right);
    });
  }, [rows, sort]);

  return { rows: sorted, sort, toggle };
}

export function DataTable<Row extends Record<string, unknown>>(props: {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  selected?: number;
  onSelect?: (index: number) => void;
  /** The column the rows are ordered by, and the way to change it.
   *  Both absent leaves the headings as plain text. */
  sort?: Sort | null;
  onSort?: (key: string) => void;
}): ReactElement {
  const { columns, rows, rowKey, selected, onSelect, sort, onSort } = props;
  return (
    <div className="table-scroll">
      <table className="grid">
        <thead>
          <tr>
            {columns.map((column) => {
              const sortable = onSort !== undefined && column.sortable !== false;
              const active = sort?.key === column.key ? sort.direction : null;
              return (
                <th
                  scope="col"
                  key={column.key}
                  className={column.align === "right" ? "num" : undefined}
                  aria-sort={
                    active === null
                      ? undefined
                      : active === SortDirection.Asc
                        ? "ascending"
                        : "descending"
                  }
                >
                  {sortable ? (
                    <button
                      type="button"
                      className="th-sort"
                      onClick={() => onSort(column.key)}
                      title={`Sort by ${column.label}`}
                    >
                      {column.label}
                      <span aria-hidden="true" className="th-arrow">
                        {active === SortDirection.Asc ? "▲" : active === SortDirection.Desc ? "▼" : ""}
                      </span>
                    </button>
                  ) : (
                    column.label
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              className={selected === index ? "row-selected" : undefined}
              // `aria-current`, not `aria-selected`: the latter is only
              // meaningful inside a `grid`/`treegrid`, and on a plain table
              // it is ignored -- leaving the visual selection with no
              // accessible counterpart at all.
              aria-current={selected === index ? "true" : undefined}
              onClick={() => onSelect?.(index)}
            >
              {columns.map((column) => (
                <td key={column.key} className={column.align === "right" ? "num" : undefined}>
                  {column.format ? column.format(row) : String(row[column.key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A `DataTable` that sorts itself.
 *
 *  For the tables nobody walks with j/k -- an analyst section, a
 *  statement -- where there is no selection for the order to disagree
 *  with, so the hook can live inside the table after all. */
export function SortedTable<Row extends Record<string, unknown>>(props: {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
}): ReactElement {
  const { rows, sort, toggle } = useSortedRows(props.rows);
  return (
    <DataTable columns={props.columns} rows={rows} rowKey={props.rowKey} sort={sort} onSort={toggle} />
  );
}

//: Anything that answers a keypress itself. A `<button>` keeps focus
//: after a click, so without this Enter would re-click "Load more" AND
//: open the selected row, and a tab click would leave j/k firing while
//: the tab still has focus.
const INTERACTIVE = "input, textarea, select, button, a, [contenteditable], [role=tab]";

/** j/k/Enter over `count` rows while nothing interactive is focused, and
 *  only in the panel the keyboard is talking to. The listener is on
 *  `window` because rows are not focusable, so with two lists open both
 *  would move on one `j`; the frame settles which meant it. */
export function useListKeys(
  count: number,
  onEnter: (index: number) => void,
): [selected: number, setSelected: (index: number) => void] {
  const [selected, setSelected] = useState(0);
  const focused = usePanelFocus();
  const focusedRef = useRef(focused);
  focusedRef.current = focused;
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  useEffect(() => {
    setSelected((s) => Math.min(Math.max(s, 0), Math.max(0, count - 1)));
  }, [count]);

  useEffect(() => {
    function handler(event: KeyboardEvent) {
      if (!focusedRef.current || event.defaultPrevented) return;
      // A control may blur itself before this window listener runs.
      if (event.target instanceof Element && event.target.closest(INTERACTIVE)) return;
      const el = document.activeElement;
      if (el !== null && el !== document.body && el.closest(INTERACTIVE) !== null) return;
      if (event.key === "j") setSelected((s) => Math.min(count - 1, s + 1));
      else if (event.key === "k") setSelected((s) => Math.max(0, s - 1));
      else if (event.key === "Enter") onEnterRef.current(selectedRef.current);
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [count]);

  return [selected, setSelected];
}
