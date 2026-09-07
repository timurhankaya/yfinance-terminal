import { useEffect, useState } from "react";
import { getDataset, getDatasetPage, type Row as ApiRow } from "../api/client";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, MissingCard, useListKeys, usePanelData } from "./common";
import { edgarUrl } from "./links";

type Row = ApiRow;

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

function text(row: Row, key: string): string {
  const v = row[key];
  return v === null || v === undefined ? "—" : String(v);
}

function Exhibits({ row, attached }: { row: Row; attached: Row[] }) {
  const edgar = edgarUrl(row.filing_id);
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

function FilingList({ data, onLoadMore }: { data: Filings; onLoadMore: (() => void) | null }) {
  const { filings, exhibits } = data;
  const [open, setOpen] = useState<number | null>(null);
  const toggle = (index: number) => setOpen((current) => (current === index ? null : index));
  const [selected, setSelected] = useListKeys(filings.length, toggle);
  const opened = open === null ? undefined : filings[open];

  return (
    <div className="split">
      <ul className="list split-list" role="listbox" aria-label="filings">
        {filings.map((row, index) => {
          const id = String(row.filing_id ?? index);
          // Yahoo's archived filing page (edgar_url) answers 404 today; the
          // SEC's own folder for the accession number is the durable link.
          const edgar = edgarUrl(row.filing_id);
          return (
            <li
              key={id}
              role="option"
              tabIndex={-1}
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
            <button type="button" className="fn" onClick={onLoadMore}>
              Load more
            </button>
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
  const [extra, setExtra] = useState<{ key: string; rows: Row[]; cursor: string | null }>({ key, rows: [], cursor: null });
  useEffect(() => setExtra({ key, rows: [], cursor: null }), [key]);
  if (symbol === null) return null;
  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <MissingCard symbol={symbol} />;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === "empty") return <EmptyCard what="filings" />;
  const current = extra.key === key ? extra : { key, rows: [], cursor: null };
  const cursor = current.rows.length > 0 ? current.cursor : state.data.next_cursor;
  const data: Filings = { ...state.data, filings: [...state.data.filings, ...current.rows] };
  const loadMore = async () => {
    if (!cursor) return;
    const page = await getDatasetPage("sec_filings", filingParams(symbol, filingType), cursor);
    setExtra((e) => (e.key === key ? { ...e, rows: [...e.rows, ...page.rows], cursor: page.next_cursor } : e));
  };
  return <FilingList data={data} onLoadMore={cursor ? () => void loadMore() : null} />;
}

export const CF_PANEL: PanelSpec = {
  code: "CF",
  title: "SEC filings",
  needsSymbol: true,
  layout: "single",
  parseArgs,
  component: CF,
};
