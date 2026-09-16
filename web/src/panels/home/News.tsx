// The archive's newest headlines, market-wide. `news` is keyed for the
// API by `(pub_date, news_id)` DESCENDING, so the first page IS the
// latest. The panel `N` is per symbol.
import { getDatasetPage } from "../../api/client";
import type { Row } from "../../api/client";
import { LoadState, usePanelData } from "../common";
import { Block, Failed, Waiting } from "./Block";

const HEADLINES = 15;

function text(row: Row, key: string): string {
  const value = row[key];
  return typeof value === "string" ? value : "";
}

/** `HH:MM UTC`, or the date when it is not today: a headline from last
 *  week saying "09:12" would read as this morning. */
function when(value: string, now: Date): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "";
  const sameDay = at.toISOString().slice(0, 10) === now.toISOString().slice(0, 10);
  return sameDay ? `${at.toISOString().slice(11, 16)} UTC` : at.toISOString().slice(0, 10);
}

export function News({ onOpen }: { onOpen: () => void }) {
  return (
    <Block title="Latest news" onOpen={onOpen}>
      {(visible) => (visible ? <NewsBody /> : <Waiting what="headlines" />)}
    </Block>
  );
}

function NewsBody() {
  const { state } = usePanelData<Row[]>(
    "home-news",
    async () => (await getDatasetPage("news", {}, null, HEADLINES)).rows,
    (rows) => rows.length === 0,
  );
  if (state.kind === LoadState.Loading) return <Waiting what="headlines" />;
  if (state.kind === LoadState.Error) return <Failed what="News" message={state.message} />;
  if (state.kind === LoadState.Empty) return <p className="muted">No news in the archive yet.</p>;
  if (state.kind !== LoadState.Ready) return null;

  const now = new Date();
  return (
    <ul className="list home-news">
      {state.data.map((row) => {
        const url = text(row, "canonical_url") || text(row, "click_through_url");
        const title = text(row, "title");
        return (
          <li key={text(row, "news_id")} className="list-row">
            <span className="muted home-when">{when(text(row, "pub_date"), now)}</span>{" "}
            {url === "" ? (
              title
            ) : (
              <a href={url} target="_blank" rel="noopener noreferrer">
                {title}
              </a>
            )}{" "}
            <span className="muted">{text(row, "provider_name")}</span>
          </li>
        );
      })}
    </ul>
  );
}
