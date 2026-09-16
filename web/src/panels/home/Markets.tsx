// Every market the archive watches, not a slice of them.
//
// `market_summary` holds every region and board Yahoo quotes, so the page
// shows all of them and the reader sorts.
import { PAGE_LIMIT, getDatasetPage } from "../../api/client";
import type { Row } from "../../api/client";
import { DataTable, LoadState, usePanelData, useSortedRows, type Column } from "../common";
import { asNumber, formatPrice, text } from "../format";
import { SparkCell, sparkLabel, useSparklines } from "../spark";
import type { SparkData } from "../spark";
import { Block, Failed, Waiting } from "./Block";

interface MarketRow extends Record<string, unknown> {
  symbol: string;
  name: string;
  price: string | null;
  change_percent: number | null;
  state: string;
}

function marketRows(rows: Row[]): MarketRow[] {
  return rows.map((row) => ({
    symbol: text(row, "symbol"),
    name: text(row, "short_name"),
    price: typeof row.regular_market_price === "string" ? row.regular_market_price : null,
    change_percent: asNumber(row.regular_market_change_percent),
    state: typeof row.market_state === "string" ? row.market_state.toLowerCase() : "—",
  }));
}

function columns(spark: SparkData): Column<MarketRow>[] {
  return [
    { key: "symbol", label: "Symbol" },
    { key: "name", label: "Name" },
    { key: "price", label: "Last", align: "right", format: (row) => formatPrice(row.price) },
    {
      key: "change_percent",
      label: "Change %",
      align: "right",
      format: (row) => <Move percent={row.change_percent} />,
    },
    {
      key: "__spark",
      label: sparkLabel(spark),
      sortable: false,
      format: (row) => <SparkCell data={spark} symbol={row.symbol} />,
    },
    { key: "state", label: "Session" },
  ];
}

function Move({ percent }: { percent: number | null }) {
  if (percent === null) return <span className="muted">—</span>;
  const way = percent > 0 ? "up" : percent < 0 ? "down" : undefined;
  return (
    <span className={way}>
      {percent > 0 ? "+" : ""}
      {percent.toFixed(2)}%
    </span>
  );
}

export function Markets({ onOpen }: { onOpen: (symbol: string) => void }) {
  return (
    <Block title="Markets" onOpen={() => onOpen("")} wide>
      {(visible) => (visible ? <MarketsBody onOpen={onOpen} /> : <Waiting what="markets" />)}
    </Block>
  );
}

function MarketsBody({ onOpen }: { onOpen: (symbol: string) => void }) {
  const { state } = usePanelData<Row[]>(
    "home-markets",
    async () => (await getDatasetPage("market_summary", {}, null, PAGE_LIMIT)).rows,
    (rows) => rows.length === 0,
  );
  const raw = state.kind === LoadState.Ready ? state.data : EMPTY;
  const rows = marketRows(raw);
  const spark = useSparklines(rows.map((row) => row.symbol).filter((symbol) => symbol !== ""));
  const { rows: ordered, sort, toggle } = useSortedRows(rows);

  if (state.kind === LoadState.Loading) return <Waiting what="markets" />;
  if (state.kind === LoadState.Error) return <Failed what="Markets" message={state.message} />;
  if (state.kind === LoadState.Empty) return <p className="muted">No market quotes in the archive yet.</p>;

  return (
    <div className="scroll-x">
      <DataTable
        columns={columns(spark)}
        rows={ordered}
        rowKey={(row) => row.symbol}
        sort={sort}
        onSort={toggle}
        onSelect={(index) => {
          const row = ordered[index];
          if (row !== undefined) onOpen(row.symbol);
        }}
      />
    </div>
  );
}

const EMPTY: Row[] = [];
