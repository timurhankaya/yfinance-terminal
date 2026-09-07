// QR: time and sales. The archive's last few hundred ticks, and then
// whatever the socket says, in one list.
//
// One row shape for both halves. `/ui/api/.../ticks` returns exactly the
// body the socket sends, from the same field table, so the opening page
// and the live rows are indistinguishable once rendered -- there is no
// seam at the join, and no second formatter to keep in step.
//
// A break line marks where this page lost its socket. Ticks that arrived
// during an outage were never delivered and are not backfilled: the tape
// is what this connection saw, and a silent join across a gap would read
// as a quiet market.
import { useEffect, useMemo, useRef, useState } from "react";
import { getTicks, TICKS_DEFAULT } from "../api/client";
import { useLinkState, useLiveEnabled, useTape } from "../live/hooks";
import { LinkState } from "../live/types";
import type { Tick } from "../live/types";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { Layout } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { formatDecimal, formatInteger } from "./table";

//: One shared empty array, so "no rows yet" keeps its identity.
const NO_TICKS: Tick[] = [];

const MAX_ROWS = 2000;
export const QR_ARGS = `QR [rows 1-${MAX_ROWS}]`;
export const QR_USAGE = `Usage: ${QR_ARGS}`;
//: Rows on screen at once. The tape holds up to 2,000; this is what is
//: rendered, and Enter or the button grows it. A tape is read at the
//: top -- the rest is scrollback, and paying to lay out 2,000 rows for
//: the twenty anyone looks at is what makes a terminal feel slow.
const PAGE = 300;

function rowsInRange(rows: number): boolean {
  return Number.isInteger(rows) && rows >= 1 && rows <= MAX_ROWS;
}

function parseArgs(tokens: string[]): PanelArgs {
  if (tokens.length === 0) return {};
  const rows = Number(tokens[0]);
  if (!rowsInRange(rows)) throw new Error(QR_USAGE);
  return { rows: String(rows) };
}

function key(tick: Tick): string {
  return `${tick.s}|${tick.t}`;
}

/** `HH:MM:SS UTC`, to the second. Milliseconds are in the archive and
 *  matter for ordering, but a column of them is unreadable. */
export function tapeClock(epochMs: number): string {
  return new Date(epochMs).toISOString().slice(11, 19);
}

export interface TapeItem {
  tick: Tick;
  /** True when the socket dropped just before this row arrived. */
  breakBefore: boolean;
}

/** The tape and the opening page as one list, newest first.
 *
 *  The socket republishes rows that `ON CONFLICT DO NOTHING` dropped, and
 *  the opening page overlaps whatever arrived while it was in flight, so
 *  the same `(symbol, t)` reaches here from both sides. */
export function mergeTape(tape: Tick[], history: Tick[], breaks: Set<string>): TapeItem[] {
  const seen = new Set<string>();
  const items: TapeItem[] = [];
  for (const tick of [...tape, ...history]) {
    const id = key(tick);
    if (seen.has(id)) continue;
    seen.add(id);
    items.push({ tick, breakBefore: breaks.has(id) });
  }
  return items;
}

export function QR({ symbol, args }: PanelProps) {
  const rows = Number(args.rows ?? TICKS_DEFAULT);
  const tape = useTape(symbol);
  const link = useLinkState();
  const enabled = useLiveEnabled();
  const [shown, setShown] = useState(PAGE);
  const [breaks, setBreaks] = useState<Set<string>>(new Set());
  const wasOpen = useRef(link === LinkState.Open);

  const { state, retry } = usePanelData<Tick[]>(
    `${symbol ?? ""}|${rows}`,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getTicks(symbol, rows)),
  );

  // The break is recorded against the newest row the page had when the
  // socket went down, so it stays anchored to that instant as the list
  // grows above it.
  useEffect(() => {
    const open = link === LinkState.Open;
    if (wasOpen.current && !open) {
      const newest = tape[0];
      if (newest !== undefined) {
        setBreaks((current) => new Set(current).add(key(newest)));
      }
    }
    wasOpen.current = open;
  }, [link, tape]);

  const history = useMemo(
    () => (state.kind === LoadState.Ready ? state.data : NO_TICKS),
    [state],
  );
  const items = useMemo(() => mergeTape(tape, history, breaks), [tape, history, breaks]);

  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol} ticks…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (items.length === 0) {
    return (
      <section>
        <EmptyCard what="ticks" />
        <p className="muted">
          {enabled
            ? `${symbol} is not in the stream's scope, so no ticks are archived for it. ` +
              "`yfin stream scope add` is what puts it there."
            : "The live stream is off, and the archive holds no ticks for this symbol."}
        </p>
      </section>
    );
  }

  const visible = items.slice(0, shown);
  return (
    <section>
      <p className="chart-note">
        <span>
          {symbol} · time and sales · {items.length} ticks · UTC
        </span>
        {!enabled && <span className="muted">live stream off; this is the archive</span>}
        {enabled && link !== LinkState.Open && <span className="strip-link">reconnecting</span>}
      </p>
      <div className="tape-head">
        <span>Time</span>
        <span className="num">Price</span>
        <span className="num">Size</span>
        <span className="num">Day volume</span>
      </div>
      <ul className="list" aria-label={`${symbol} time and sales`}>
        {visible.map((item) => (
          <li key={key(item.tick)}>
            {item.breakBefore && (
              <p className="tape-break" role="separator">
                — connection lost; ticks in this window were not delivered —
              </p>
            )}
            <span className="tape-line">
              <span>{tapeClock(item.tick.t)}</span>
              <span className="num">{formatDecimal(item.tick.p)}</span>
              <span className="num">{item.tick.ls === undefined ? "—" : formatInteger(item.tick.ls)}</span>
              <span className="num">{item.tick.v === undefined ? "—" : formatInteger(item.tick.v)}</span>
            </span>
          </li>
        ))}
      </ul>
      {shown < items.length && (
        <p className="load-more">
          <button type="button" onClick={() => setShown((count) => count + PAGE)}>
            Show {Math.min(PAGE, items.length - shown)} more
          </button>{" "}
          <span className="muted">
            {visible.length} of {items.length}
          </span>
        </p>
      )}
    </section>
  );
}

export const QR_PANEL: PanelSpec = {
  code: "QR",
  title: "Time and sales, live",
  usage: QR_ARGS,
  needsSymbol: true,
  layout: Layout.Headed,
  parseArgs,
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  normalizeArgs: (args) => {
    const rows = Number(args.rows ?? TICKS_DEFAULT);
    return { ...args, rows: String(rowsInRange(rows) ? rows : TICKS_DEFAULT) };
  },
  component: QR,
};
