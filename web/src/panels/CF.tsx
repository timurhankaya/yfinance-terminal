import { useState } from "react";
import { getDataset, getDatasetPage, type Row as ApiRow } from "../api/client";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import {
  EmptyCard,
  ErrorCard,
  LoadState,
  MissingCard,
  NextPageError,
  useListKeys,
  usePagedRows,
  usePanelData,
} from "./common";
import { text } from "./format";
import { edgarUrl } from "./links";

type Row = ApiRow;

export const CF_ARGS = "CF [filing type]";
export const CF_USAGE = `Usage: ${CF_ARGS}`;

interface Filings {
  filings: Row[];
  next_cursor: string | null;
  //: filing_id -> exhibits. The exhibits feed cannot be filtered by
  //: filing, so one 200-row page is fetched and grouped here; a filing
  //: whose exhibits fall outside that page shows a note, not a lie.
  exhibits: Map<string, Row[]>;
}

function parseArgs(tokens: string[]): PanelArgs {
  const type = tokens[0];
  return type === undefined ? {} : { filing_type: type.toUpperCase() };
}

function filingParams(symbol: string, filingType: string | undefined): Record<string, string> {
  return { symbol: symbol.trim().toUpperCase(), ...(filingType ? { filing_type: filingType } : {}) };
}

async function loadFilings(symbol: string, filingType: string | undefined): Promise<Filings> {
  const [page, exhibitRows] = await Promise.all([
    getDatasetPage("sec_filings", filingParams(symbol, filingType)),
    getDataset("sec_filing_exhibits", symbol),
  ]);
  const filings = page.rows;
  const exhibits = new Map<string, Row[]>();
  for (const row of exhibitRows) {
    const id = String(row.filing_id ?? "");
    const bucket = exhibits.get(id);
    if (bucket) bucket.push(row);
    else exhibits.set(id, [row]);
  }
  return { filings, next_cursor: page.next_cursor, exhibits };
}

function Exhibits({ row, attached }: { row: Row; attached: Row[] }) {
  const edgar = edgarUrl(row.filing_id, row.edgar_url);
  return (
    <article className="detail" aria-label="exhibits">
      <h3>
        {text(row, "filing_type")} · {text(row, "filing_date")}
      </h3>
      <p className="detail-meta">{text(row, "title")}</p>
      {edgar !== null && (
        <p>
          <a href={edgar} target="_blank" rel="noopener noreferrer">
            Open on EDGAR ↗
          </a>
        </p>
      )}
      {attached.length === 0 ? (
        <p className="muted">No exhibits fetched for this filing (the exhibits feed is capped at 200 rows).</p>
      ) : (
        <ul className="list">
          {attached.map((ex) => (
            <li key={String(ex.url_hash ?? ex.url)} className="list-row">
              <span>{text(ex, "exhibit_type")}</span>{" "}
              {typeof ex.url === "string" && /^https?:\/\//i.test(ex.url) && (
                <a href={ex.url} target="_blank" rel="noopener noreferrer">
                  {ex.url}
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

function FilingList(props: {
  data: Filings;
  onLoadMore: (() => void) | null;
  loadingMore: boolean;
  pageError: string | null;
}) {
  const { data, onLoadMore, loadingMore, pageError } = props;
  const { filings, exhibits } = data;
  const [open, setOpen] = useState<number | null>(null);
  const toggle = (index: number) => setOpen((current) => (current === index ? null : index));
  const [selected, setSelected] = useListKeys(filings.length, toggle);
  const opened = open === null ? undefined : filings[open];

  return (
    <div className="split">
      {/* Focusable, with the active option named: the j/k/Enter model
          lives on a window listener, so without these the whole keyboard
          interaction is unreachable by Tab and invisible to a reader. */}
      <ul
        className="list split-list"
        role="listbox"
        aria-label="filings"
        tabIndex={0}
        aria-activedescendant={filings.length > 0 ? `cf-filing-${selected}` : undefined}
      >
        {filings.map((row, index) => {
          const id = String(row.filing_id ?? index);
          // Yahoo's archived filing page (edgar_url) answers 404 today; the
          // SEC's own folder for the accession number is the durable link.
          const edgar = edgarUrl(row.filing_id, row.edgar_url);
          return (
            <li
              key={id}
              id={`cf-filing-${index}`}
              role="option"
              className={index === selected ? "list-row row-selected" : "list-row"}
              aria-selected={index === selected}
              onClick={() => {
                setSelected(index);
                toggle(index);
              }}
            >
              <span className="muted">{text(row, "filing_date")}</span>{" "}
              <span>{text(row, "filing_type")}</span>{" "}
              <span>{text(row, "title")}</span>{" "}
              <span className="muted">
                {text(row, "exhibit_count")} {text(row, "exhibit_count") === "1" ? "exhibit" : "exhibits"}
              </span>{" "}
              {edgar !== null && (
                <a href={edgar} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
                  EDGAR ↗
                </a>
              )}
            </li>
          );
        })}
        {onLoadMore && (
          <li className="load-more">
            <button type="button" className="fn" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          </li>
        )}
        {pageError !== null && (
          <li>
            <NextPageError message={pageError} />
          </li>
        )}
      </ul>
      <div className="split-detail">
        {opened ? (
          <Exhibits row={opened} attached={exhibits.get(String(opened.filing_id ?? "")) ?? []} />
        ) : (
          <p className="muted">Enter or click opens a filing's exhibits here.</p>
        )}
      </div>
    </div>
  );
}

export function CF({ symbol, args }: PanelProps) {
  const filingType = args.filing_type;
  const key = `${symbol ?? ""}|${filingType ?? ""}`;
  const { state, retry } = usePanelData<Filings>(
    key,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : loadFilings(symbol, filingType)),
    (data) => data.filings.length === 0,
  );
  const paged = usePagedRows<Row>(key, state.kind === LoadState.Ready ? state.data.next_cursor : null, (cursor) =>
    symbol === null
      ? Promise.reject(new Error("no symbol"))
      : getDatasetPage("sec_filings", filingParams(symbol, filingType), cursor),
  );
  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="filings" />;
  const data: Filings = { ...state.data, filings: [...state.data.filings, ...paged.rows] };
  return (
    <FilingList
      data={data}
      onLoadMore={paged.cursor === null ? null : paged.loadMore}
      loadingMore={paged.loadingMore}
      pageError={paged.error}
    />
  );
}

export const CF_PANEL: PanelSpec = {
  code: "CF",
  title: "SEC filings",
  usage: CF_ARGS,
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs,
  component: CF,
};
