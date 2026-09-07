import { useEffect, useRef, useState } from "react";
import { getNews, type NewsItem } from "../api/client";
import type { PanelProps, PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, MissingCard, useListKeys, usePanelData } from "./common";

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function isHttpUrl(value: string | null): value is string {
  return value !== null && /^https?:\/\//i.test(value);
}

function Detail({ article }: { article: NewsItem }) {
  const link = isHttpUrl(article.link) ? article.link : null;
  const thumb = isHttpUrl(article.thumbnail_url) ? article.thumbnail_url : null;
  return (
    <article className="detail" aria-label="article">
      {thumb && <img className="thumb" src={thumb} alt="" />}
      <h3>{link ? <a href={link} target="_blank" rel="noopener noreferrer">{article.title}</a> : article.title}</h3>
      <p className="detail-meta">
        {article.provider_name ?? "Unknown source"} · {when(article.pub_date)}
      </p>
      <p>{article.summary ?? "No summary."}</p>
      {link && (
        <a href={link} target="_blank" rel="noopener noreferrer">
          Open article ↗
        </a>
      )}
    </article>
  );
}

function NewsList({ rows }: { rows: NewsItem[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const [selected, setSelected] = useListKeys(rows.length, (index) => setOpen(index));
  const listRef = useRef<HTMLUListElement>(null);
  const article = open === null ? undefined : rows[open];

  // Keep the keyboard selection in view; a 50-row list scrolls past the fold.
  useEffect(() => {
    const el = listRef.current?.children[selected];
    if (el instanceof HTMLElement && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest" });
    }
  }, [selected]);

  return (
    <div className="split">
      <ul className="list split-list" role="listbox" aria-label="news" ref={listRef}>
        {rows.map((row, index) => (
          <li
            key={row.news_id}
            role="option"
            tabIndex={-1}
            className={index === selected ? "list-row row-selected" : "list-row"}
            aria-selected={index === selected}
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
      <div className="split-detail">
        {article ? <Detail article={article} /> : <p className="muted">Enter or click opens an article here.</p>}
      </div>
    </div>
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
