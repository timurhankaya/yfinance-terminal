import { useEffect, useRef, useState } from "react";
import { NEWS_MAX, NEWS_PAGE, getNews, type NewsItem } from "../api/client";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, useKeptData, useListKeys, usePanelData } from "./common";
import { isHttpUrl } from "./format";

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
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
      {/* Focusable, with the active option named: the j/k/Enter model
          lives on a window listener, so without these the whole keyboard
          interaction is unreachable by Tab and invisible to a reader. */}
      <ul
        className="list split-list"
        role="listbox"
        aria-label="news"
        ref={listRef}
        tabIndex={0}
        aria-activedescendant={rows.length > 0 ? `n-article-${selected}` : undefined}
      >
        {rows.map((row, index) => (
          <li
            key={row.news_id}
            id={`n-article-${index}`}
            role="option"
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
  // "Load more" grows the page, which changes the load's key -- and a
  // plain `Loading` there would unmount `NewsList`, closing the article
  // the reader was reading and dropping the selection back to row 0. The
  // symbol alone is the identity, so only a new symbol clears the list.
  const rows = useKeptData(symbol ?? "", state);
  if (symbol === null) return null;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="news" />;
  if (rows === null) return <p className="muted">Loading {symbol}…</p>;
  const more = rows.length >= limit && limit < NEWS_MAX;
  return <NewsList rows={rows} onLoadMore={more ? () => setLimit(Math.min(NEWS_MAX, limit + NEWS_PAGE)) : null} />;
}

export const N_PANEL: PanelSpec = {
  code: "N",
  title: "News",
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: N,
};
