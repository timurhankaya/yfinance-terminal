import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { ApiError } from "../api/client";

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
 *  in flight.
 *
 *  `usePanelData` goes back to `Loading` on every re-run, which unmounts
 *  whatever was on screen. That is right for a new query and wrong for a
 *  refresh of the one already showing: a chart loses the reader's pan and
 *  zoom, a list loses its selection and its open row. `key` is the
 *  query's IDENTITY -- the symbol and the interval, not the reload
 *  counter -- so a genuinely different query still drops the old payload
 *  rather than showing it under the new heading. */
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
  rows: T[];
  cursor: string | null;
  loading: boolean;
  error: string | null;
}

/** Cursor paging on top of a first page some other loader fetched.
 *
 *  One mechanism, one place: the reset on a new query, the guard against
 *  a second in-flight page, and the error the button has to report are
 *  the same for every list that pages by cursor. `key` identifies the
 *  query; a new one drops the pages already read. */
export function usePagedRows<T>(
  key: string,
  firstCursor: string | null,
  fetchPage: (cursor: string) => Promise<{ rows: T[]; next_cursor: string | null }>,
): PagedRows<T> {
  const [paged, setPaged] = useState<Paged<T>>({ key, rows: [], cursor: null, loading: false, error: null });
  // A ref, not a dep: callers pass an inline lambda, and a new function
  // every render must not reset the pages.
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;

  useEffect(() => {
    setPaged({ key, rows: [], cursor: null, loading: false, error: null });
  }, [key]);

  // The state can be one render behind the key; read it as empty until
  // the effect above catches up. Memoised so `loadMore` below keeps its
  // identity across renders that changed nothing.
  const current = useMemo<Paged<T>>(
    () => (paged.key === key ? paged : { key, rows: [], cursor: null, loading: false, error: null }),
    [paged, key],
  );
  // Once a page has been read, its `next_cursor` is the truth -- null
  // included, which is what "no more pages" looks like.
  const cursor = current.rows.length > 0 || current.cursor !== null ? current.cursor : firstCursor;

  const loadMore = useCallback(() => {
    if (cursor === null || current.loading) return;
    setPaged({ ...current, loading: true, error: null });
    void (async () => {
      try {
        const page = await fetchRef.current(cursor);
        setPaged((p) =>
          p.key === key
            ? { ...p, rows: [...p.rows, ...page.rows], cursor: page.next_cursor, loading: false }
            : p,
        );
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setPaged((p) => (p.key === key ? { ...p, loading: false, error: message } : p));
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

export function EmptyCard(props: { what: string }): ReactElement {
  return <p className="card card-empty">No {props.what} for this symbol.</p>;
}

export function MissingCard(props: { symbol: string | null }): ReactElement {
  return <p className="card card-error">No such symbol: {props.symbol}</p>;
}

export interface Column<Row> {
  key: string;
  label: string;
  align?: "left" | "right";
  format?: (row: Row) => ReactNode;
}

export function DataTable<Row extends Record<string, unknown>>(props: {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  selected?: number;
  onSelect?: (index: number) => void;
}): ReactElement {
  const { columns, rows, rowKey, selected, onSelect } = props;
  return (
    <table className="grid">
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.key} className={column.align === "right" ? "num" : undefined}>
              {column.label}
            </th>
          ))}
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
  );
}

//: Anything that answers a keypress itself. A `<button>` keeps focus
//: after a click, so without this Enter would re-click "Load more" AND
//: open the selected row, and a tab click would leave j/k firing while
//: the tab still has focus.
const INTERACTIVE = "input, textarea, select, button, a, [contenteditable], [role=tab]";

/** j/k/Enter over `count` rows while nothing interactive is focused. */
export function useListKeys(
  count: number,
  onEnter: (index: number) => void,
): [selected: number, setSelected: (index: number) => void] {
  const [selected, setSelected] = useState(0);
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  useEffect(() => {
    setSelected((s) => Math.min(Math.max(s, 0), Math.max(0, count - 1)));
  }, [count]);

  useEffect(() => {
    function handler(event: KeyboardEvent) {
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
