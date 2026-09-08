// What the archive says is coming, across all four calendars.
//
// Each calendar is keyed DESCENDING by its own date column, so the first
// page is the furthest future rather than the nearest -- but each of them
// fits in one page (measured 2026-09-08: 519, 534, 536 and 26 rows), so
// the whole calendar is read and the next events are picked here. A page
// that comes back full says so rather than pretending it is everything.
import { PAGE_LIMIT, getDatasetPage } from "../../api/client";
import type { Row } from "../../api/client";
import { LoadState, usePanelData } from "../common";
import { Bars } from "../viz";
import { Block, Failed, Waiting } from "./Block";

/** The four calendars and the column each one dates its events by. */
const CALENDARS: ReadonlyArray<{ name: string; when: string; kind: string; who: string }> = [
  { name: "calendar_earnings", when: "event_start_ts_utc", kind: "earnings", who: "symbol" },
  { name: "calendar_economic", when: "event_time_utc", kind: "economic", who: "event_name" },
  { name: "calendar_ipo", when: "ipo_date_utc", kind: "IPO", who: "symbol" },
  { name: "calendar_splits", when: "payable_on_utc", kind: "split", who: "symbol" },
];

const AHEAD_DAYS = 10;
const EVENTS_SHOWN = 12;

export interface Ahead {
  events: Event[];
  perDay: { day: string; count: number }[];
  truncated: string[];
}

interface Event {
  at: number;
  day: string;
  kind: string;
  who: string;
}

function text(row: Row, key: string): string {
  const value = row[key];
  return typeof value === "string" ? value : "";
}

/** Events from now to `AHEAD_DAYS` out, nearest first. */
export function ahead(pages: { rows: Row[]; full: boolean; kind: string; when: string; who: string }[], now: number): Ahead {
  const horizon = now + AHEAD_DAYS * 86_400_000;
  const events: Event[] = [];
  const truncated: string[] = [];
  for (const page of pages) {
    if (page.full) truncated.push(page.kind);
    for (const row of page.rows) {
      const at = Date.parse(text(row, page.when));
      if (Number.isNaN(at) || at < now || at > horizon) continue;
      events.push({
        at,
        day: new Date(at).toISOString().slice(0, 10),
        kind: page.kind,
        who: text(row, page.who),
      });
    }
  }
  events.sort((a, b) => a.at - b.at);

  const counts = new Map<string, number>();
  for (let offset = 0; offset < AHEAD_DAYS; offset++) {
    counts.set(new Date(now + offset * 86_400_000).toISOString().slice(0, 10), 0);
  }
  for (const event of events) {
    if (counts.has(event.day)) counts.set(event.day, (counts.get(event.day) ?? 0) + 1);
  }
  return {
    events,
    perDay: [...counts].map(([day, count]) => ({ day, count })),
    truncated,
  };
}

export function Week({ onOpen }: { onOpen: () => void }) {
  return (
    <Block title="Next ten days" onOpen={onOpen}>
      {(visible) => (visible ? <WeekBody /> : <Waiting what="calendars" />)}
    </Block>
  );
}

function WeekBody() {
  const { state } = usePanelData<Ahead>(
    "home-week",
    async () => {
      const pages = await Promise.all(
        CALENDARS.map(async (calendar) => {
          const page = await getDatasetPage(calendar.name, {}, null, PAGE_LIMIT);
          return {
            rows: page.rows,
            full: page.rows.length >= PAGE_LIMIT,
            kind: calendar.kind,
            when: calendar.when,
            who: calendar.who,
          };
        }),
      );
      return ahead(pages, Date.now());
    },
    (data) => data.events.length === 0,
  );

  if (state.kind === LoadState.Loading) return <Waiting what="calendars" />;
  if (state.kind === LoadState.Error) return <Failed what="Calendars" message={state.message} />;
  if (state.kind === LoadState.Empty) {
    return <p className="muted">The calendars hold nothing in the next ten days.</p>;
  }
  if (state.kind !== LoadState.Ready) return null;

  const { events, perDay, truncated } = state.data;
  return (
    <>
      <Bars
        categories={perDay.map((day) => day.day.slice(5))}
        series={[{ key: "events", label: "Events", values: perDay.map((day) => day.count) }]}
        label="Events per day over the next ten days"
        format={(value) => String(Math.round(value))}
      />
      <ul className="list">
        {events.slice(0, EVENTS_SHOWN).map((event) => (
          <li key={`${event.kind}-${event.who}-${String(event.at)}`} className="list-row">
            <span className="muted home-when">{event.day.slice(5)}</span>{" "}
            <span className="ds-name">{event.who}</span> <span className="muted">{event.kind}</span>
          </li>
        ))}
      </ul>
      {truncated.length > 0 && (
        <p className="muted">
          {truncated.join(", ")}: showing the newest page only — the calendar is longer than one
          page now.
        </p>
      )}
    </>
  );
}
