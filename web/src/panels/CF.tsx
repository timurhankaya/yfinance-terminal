import { useState } from "react";
import { getDataset } from "../api/client";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, MissingCard, useListKeys, usePanelData } from "./common";

type Row = Record<string, unknown>;

interface Filings {
  filings: Row[];
  //: filing_id -> exhibits. The exhibits feed cannot be filtered by
  //: filing, so one 200-row page is fetched and grouped here; a filing
  //: whose exhibits fall outside that page shows a note, not a lie.
  exhibits: Map<string, Row[]>;
}

function parseArgs(tokens: string[]): PanelArgs {
  const type = tokens[0];
  return type === undefined ? {} : { filing_type: type.toUpperCase() };
}

async function loadFilings(symbol: string, filingType: string | undefined): Promise<Filings> {
  const [filings, exhibitRows] = await Promise.all([
    getDataset("sec_filings", symbol, filingType ? { filing_type: filingType } : {}),
    getDataset("sec_filing_exhibits", symbol),
  ]);
  const exhibits = new Map<string, Row[]>();
  for (const row of exhibitRows) {
    const id = String(row.filing_id ?? "");
    const bucket = exhibits.get(id);
    if (bucket) bucket.push(row);
    else exhibits.set(id, [row]);
  }
  return { filings, exhibits };
}

function text(row: Row, key: string): string {
  const v = row[key];
  return v === null || v === undefined ? "—" : String(v);
}

function FilingList({ data }: { data: Filings }) {
  const { filings, exhibits } = data;
  const [expanded, setExpanded] = useState<number | null>(null);
  const toggle = (index: number) => setExpanded((current) => (current === index ? null : index));
  const [selected, setSelected] = useListKeys(filings.length, toggle);

  return (
    <ul className="list" role="listbox" aria-label="filings">
      {filings.map((row, index) => {
        const id = String(row.filing_id ?? index);
        const attached = exhibits.get(id) ?? [];
        const edgar = row.edgar_url;
        return (
          <li
            key={id}
            role="option"
            tabIndex={-1}
            className={index === selected ? "list-row row-selected" : "list-row"}
            aria-selected={index === selected}
            data-expanded={index === expanded}
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
            {typeof edgar === "string" && /^https?:\/\//i.test(edgar) && (
              <a href={edgar} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
                EDGAR ↗
              </a>
            )}
            {index === expanded && (
              // Clicks inside the exhibit list must not bubble to the row
              // and collapse what the user is reading.
              <div className="detail" onClick={(e) => e.stopPropagation()}>
                {attached.length === 0 ? (
                  <p className="muted">
                    No exhibits fetched for this filing (the exhibits feed is capped at 200 rows).
                  </p>
                ) : (
                  <ul className="list">
                    {attached.map((ex) => (
                      <li key={String(ex.url_hash ?? ex.url)} className="list-row">
                        <span>{text(ex, "exhibit_type")}</span>{" "}
                        {typeof ex.url === "string" && (
                          <a href={ex.url} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
                            {ex.url}
                          </a>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function CF({ symbol, args }: PanelProps) {
  const filingType = args.filing_type;
  const { state, retry } = usePanelData<Filings>(
    `${symbol ?? ""}|${filingType ?? ""}`,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : loadFilings(symbol, filingType)),
    (data) => data.filings.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <MissingCard symbol={symbol} />;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === "empty") return <EmptyCard what="filings" />;
  return <FilingList data={state.data} />;
}

export const CF_PANEL: PanelSpec = {
  code: "CF",
  title: "SEC filings",
  needsSymbol: true,
  layout: "single",
  parseArgs,
  component: CF,
};
