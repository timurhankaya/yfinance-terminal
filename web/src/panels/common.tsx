import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { ApiError, UnauthorizedError } from "../api/client";
import { useSession } from "../app/session";

export type Loaded<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "empty" }
  | { kind: "missing" }
  | { kind: "error"; message: string };

/** Loads while the session is authenticated; re-runs when `key` changes or the session
 *  comes back after a login; drops stale responses; 401 -> requireLogin (state stays
 *  "loading" until the session returns); 404 -> missing; isEmpty(data) -> empty. */
export function usePanelData<T>(
  key: string,
  load: () => Promise<T>,
  isEmpty?: (data: T) => boolean,
): { state: Loaded<T>; retry: () => void } {
  const { me, requireLogin } = useSession();
  const authenticated = me?.authenticated === true;
  const [state, setState] = useState<Loaded<T>>({ kind: "loading" });
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
    setState({ kind: "loading" });
    try {
      const data = await loadRef.current();
      if (token.cancelled) return;
      const empty = isEmptyRef.current;
      setState(empty && empty(data) ? { kind: "empty" } : { kind: "ready", data });
    } catch (err) {
      if (token.cancelled) return;
      if (err instanceof UnauthorizedError) requireLogin(); // stays "loading"; re-runs after login
      else if (err instanceof ApiError && err.status === 404) setState({ kind: "missing" });
      else setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }, [requireLogin]);

  useEffect(() => {
    if (!authenticated) return;
    void run();
    return () => {
      tokenRef.current.cancelled = true;
    };
  }, [key, authenticated, run]);

  const retry = useCallback(() => {
    if (authenticated) void run();
  }, [authenticated, run]);

  return { state, retry };
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
            aria-selected={selected === index ? "true" : undefined}
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

/** j/k/Enter over `count` rows while no INPUT/TEXTAREA is focused. */
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
      if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return;
      if (event.key === "j") setSelected((s) => Math.min(count - 1, s + 1));
      else if (event.key === "k") setSelected((s) => Math.max(0, s - 1));
      else if (event.key === "Enter") onEnterRef.current(selectedRef.current);
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [count]);

  return [selected, setSelected];
}
