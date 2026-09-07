// PX: price bars as a table, newest first. Charts (GP/GIP) are 1d's; this
// is the archive's bars, every column, readable now.
import {
  BAR_INTERVALS,
  Interval,
  PAGE_LIMIT,
  WireType,
  getBars,
  isInterval,
  type CatalogColumn,
  type Row,
} from "../api/client";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { DatasetTable } from "./table";

export const PX_ARGS = `PX [${BAR_INTERVALS.join("|")}] [rows 1-${PAGE_LIMIT}]`;
export const PX_USAGE = `Usage: ${PX_ARGS}`;
const DEFAULT_INTERVAL = Interval.D1;
const DEFAULT_ROWS = 250;

function intervalOr(value: string | undefined, fallback: Interval): Interval {
  return value !== undefined && isInterval(value) ? value : fallback;
}

function rowsInRange(rows: number): boolean {
  return Number.isInteger(rows) && rows >= 1 && rows <= PAGE_LIMIT;
}

function parseArgs(tokens: string[]): PanelArgs {
  const interval = tokens[0] ?? DEFAULT_INTERVAL;
  const rows = tokens[1] === undefined ? DEFAULT_ROWS : Number(tokens[1]);
  if (!isInterval(interval)) throw new Error(PX_USAGE);
  if (!rowsInRange(rows)) throw new Error(PX_USAGE);
  return { interval, rows: String(rows) };
}

//: The Bar schema, as columns for the typed table.
export const BAR_COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: WireType.String, nullable: false },
  { name: "ts_utc", type: WireType.DateTime, nullable: false },
  { name: "bar_interval", type: WireType.String, nullable: true },
  { name: "session_date", type: WireType.Date, nullable: true },
  { name: "local_date", type: WireType.Date, nullable: true },
  { name: "open", type: WireType.Decimal, nullable: true },
  { name: "high", type: WireType.Decimal, nullable: true },
  { name: "low", type: WireType.Decimal, nullable: true },
  { name: "close", type: WireType.Decimal, nullable: true },
  { name: "adj_close", type: WireType.Decimal, nullable: true },
  { name: "volume", type: WireType.Integer, nullable: true },
  { name: "is_extended", type: WireType.Boolean, nullable: true },
];

export function PX({ symbol, args }: PanelProps) {
  const interval = intervalOr(args.interval, DEFAULT_INTERVAL);
  const rows = Number(args.rows ?? DEFAULT_ROWS);
  const { state, retry } = usePanelData<Row[]>(
    `${symbol ?? ""}|${interval}|${rows}`,
    () =>
      symbol === null
        ? Promise.reject(new Error("no symbol"))
        : getBars(symbol, interval, rows),
    (data) => data.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol} {interval} bars…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what={`${interval} bars`} />;
  return (
    <section>
      <p className="detail-meta">
        The newest {rows} {interval} bars, newest first. {PX_ARGS}
      </p>
      <DatasetTable columns={BAR_COLUMNS} rows={state.data} hide={["symbol"]} reverse />
    </section>
  );
}

export const PX_PANEL: PanelSpec = {
  code: "PX",
  title: "Price bars as a table",
  usage: PX_ARGS,
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs,
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  normalizeArgs: (args) => {
    const rows = Number(args.rows ?? DEFAULT_ROWS);
    return {
      ...args,
      interval: intervalOr(args.interval, DEFAULT_INTERVAL),
      rows: String(rowsInRange(rows) ? rows : DEFAULT_ROWS),
    };
  },
  component: PX,
};
