// PX: price bars as a table, newest first. Charts (GP/GIP) are 1d's; this
// is the archive's bars, every column, readable now.
import { BAR_INTERVALS, PAGE_LIMIT, WireType, getBars, type CatalogColumn, type Row } from "../api/client";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { DatasetTable } from "./table";

export const PX_USAGE = `Usage: PX [${BAR_INTERVALS.join("|")}] [rows 1-${PAGE_LIMIT}]`;
const DEFAULT_ROWS = 250;

function parseArgs(tokens: string[]): PanelArgs {
  const interval = tokens[0] ?? "1d";
  const rows = tokens[1] === undefined ? DEFAULT_ROWS : Number(tokens[1]);
  if (!BAR_INTERVALS.includes(interval)) throw new Error(PX_USAGE);
  if (!Number.isInteger(rows) || rows < 1 || rows > PAGE_LIMIT) throw new Error(PX_USAGE);
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
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  const interval = args.interval ?? "1d";
  const rows = Number(args.rows ?? DEFAULT_ROWS);
  const valid = BAR_INTERVALS.includes(interval) && Number.isInteger(rows) && rows >= 1 && rows <= PAGE_LIMIT;
  const { state, retry } = usePanelData<Row[]>(
    `${symbol ?? ""}|${interval}|${rows}`,
    () =>
      symbol === null || !valid
        ? Promise.reject(new Error(valid ? "no symbol" : PX_USAGE))
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
        The newest {rows} {interval} bars, newest first. {PX_USAGE.slice(7)}
      </p>
      <DatasetTable columns={BAR_COLUMNS} rows={state.data} hide={["symbol"]} reverse />
    </section>
  );
}

export const PX_PANEL: PanelSpec = {
  code: "PX",
  title: "Price bars as a table",
  usage: PX_USAGE.slice(7),
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs,
  component: PX,
};
