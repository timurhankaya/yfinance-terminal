import { useEffect, useRef, useState } from "react";
import { NEWS_MAX, NEWS_PAGE, getNews, type NewsItem } from "../api/client";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, useListKeys, usePanelData } from "./common";

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function isHttpUrl(value: string | null): value is string {
  return value !== null && /^https?:\/\//i.test(value);
}

function Detail({ article }: { article: NewsItem }) {
  const link = isHttpUrl(article.link) ? article.link : null;
  // https only: the page's CSP allows img-src https:, so an http:// thumbnail
  // would render as a broken image rather than being skipped.
  const thumb = article.thumbnail_url?.startsWith("https://") ? article.thumbnail_url : null;
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

function NewsList({ rows, onLoadMore }: { rows: NewsItem[]; onLoadMore: (() => void) | null }) {
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
        {onLoadMore && (
          <li className="load-more">
            <button type="button" className="fn" onClick={onLoadMore}>
              Load more
            </button>
          </li>
        )}
      </ul>
      <div className="split-detail">
        {article ? <Detail article={article} /> : <p className="muted">Enter or click opens an article here.</p>}
      </div>
    </div>
  );
}

export function N({ symbol }: PanelProps) {
  // The news route has no cursor; "Load more" asks for a bigger page,
  // up to the route's cap.
  const [limit, setLimit] = useState(NEWS_PAGE);
  useEffect(() => setLimit(NEWS_PAGE), [symbol]);
  const { state, retry } = usePanelData<NewsItem[]>(
    `${symbol ?? ""}|${limit}`,
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : getNews(symbol, limit)),
    (rows) => rows.length === 0,
  );
  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="news" />;
  const more = state.data.length >= limit && limit < NEWS_MAX;
  return <NewsList rows={state.data} onLoadMore={more ? () => setLimit(Math.min(NEWS_MAX, limit + NEWS_PAGE)) : null} />;
}

export const N_PANEL: PanelSpec = {
  code: "N",
  title: "News",
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: N,
};
