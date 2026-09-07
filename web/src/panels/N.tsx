import { useState } from "react";
import { getNews, type NewsItem } from "../api/client";
import type { PanelProps, PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, MissingCard, useListKeys, usePanelData } from "./common";

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function NewsList({ rows }: { rows: NewsItem[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const [selected, setSelected] = useListKeys(rows.length, (index) => setOpen(index));
  const article = open === null ? undefined : rows[open];

  return (
    <>
      <ul className="list" aria-label="news">
        {rows.map((row, index) => (
          <li
            key={row.news_id}
            className={index === selected ? "list-row row-selected" : "list-row"}
            aria-selected={index === selected ? "true" : undefined}
            onClick={() => {
              setSelected(index);
              setOpen(index);
            }}
          >
            <span className="muted">{when(row.pub_date)}</span>{" "}
            <span className="muted">{row.provider_name ?? "—"}</span>{" "}
            <span>{row.title}</span>
          </li>
        ))}
      </ul>
      {article && (
        <div className="detail">
          <h3>{article.title}</h3>
          <p>{article.summary ?? "No summary."}</p>
          {article.link && (
            <a href={article.link} target="_blank" rel="noopener noreferrer">
              Open article
            </a>
          )}
        </div>
      )}
    </>
  );
}

export function N({ symbol }: PanelProps) {
  const { state, retry } = usePanelData<NewsItem[]>(
    symbol ?? "",
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getNews(symbol)),
    (rows) => rows.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === "loading") return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === "missing") return <MissingCard symbol={symbol} />;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === "empty") return <EmptyCard what="news" />;
  return <NewsList rows={state.data} />;
}

export const N_PANEL: PanelSpec = {
  code: "N",
  title: "News",
  needsSymbol: true,
  layout: "single",
  parseArgs: () => ({}),
  component: N,
};
