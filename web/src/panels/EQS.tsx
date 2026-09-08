// EQS: equity screening. Without a name it lists the screens this
// deployment runs; with one it shows what that screen matched, in the
// screen's own order.
//
// `SCR` already reaches the same four tables through the generic dataset
// surface, one flat tab each. That is the guarantee that nothing is
// unreachable; this is the panel that makes them mean something
// together -- a roster you can read down, with a price beside each row
// and Enter opening the symbol.
//
// The rank order is the point. A screener's roster carries one thing
// beyond a list of tickers, which is the order the screen put them in,
// and the header says what that order is sorted by so the sequence is
// not unexplained.
import { useMemo, useState } from "react";
import type { ReactElement } from "react";
import { SCREEN_PAGE, getScreen, getScreens } from "../api/client";
import type { ScreenDetail, ScreenRow, ScreenSummary } from "../api/client";
import { usePanelRun } from "../workspace/frame";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import {
  DataTable,
  EmptyCard,
  ErrorCard,
  LoadState,
  MissingCard,
  useListKeys,
  useSortedRows,
  usePanelData,
  type Column,
} from "./common";
import { DatasetView, SymbolMode } from "./dataset";
import { SparkCell, sparkLabel, useSparklines } from "./spark";
import { formatDateTime, formatDecimal, formatInteger } from "./table";

/** A screen's sub-pages. `Members` is the roster; `Runs` is the same
 *  screen's history, which is the only place the roster's size over time
 *  and Yahoo's echoed criteria are visible. */
export enum ScreenTab {
  Members = "members",
  Runs = "runs",
}

const TABS: ReadonlyArray<[ScreenTab, string]> = [
  [ScreenTab.Members, "Members"],
  [ScreenTab.Runs, "Runs"],
];

export const EQS_USAGE = `Usage: EQS [screen] [${TABS.map(([key]) => key).join("|")}]`;

function parseArgs(tokens: string[]): PanelArgs {
  const [first, second, ...rest] = tokens;
  if (first === undefined) return {};
  if (rest.length > 0) throw new Error(EQS_USAGE);
  if (second === undefined) return { screen: first.toLowerCase() };
  const tab = TABS.find(([key]) => key === second.toLowerCase());
  if (tab === undefined) throw new Error(EQS_USAGE);
  return { screen: first.toLowerCase(), tab: tab[0] };
}

/** `2026-09-08 20:05 UTC`, or the run date alone, or why there is neither. */
export function runLabel(screen: ScreenSummary): string {
  if (screen.fetched_at !== null) return `last run ${formatDateTime(screen.fetched_at)}`;
  if (screen.as_of_date !== null) return `last run ${screen.as_of_date}`;
  // Enabled and configured, and nothing has fetched it yet. Saying so is
  // the point: an absence here would hide a misconfiguration.
  return "never run";
}

/** "120 matched, 100 kept" when the screen hit its page limit.
 *
 *  The gap between what Yahoo said matched and what was actually
 *  fetched is recorded rather than hidden, and a roster read as complete
 *  when it is not is exactly the error worth surfacing. */
export function countLabel(screen: ScreenSummary): string {
  if (screen.total === null || screen.row_count === null) return "no roster yet";
  if (screen.total === screen.row_count) return `${formatInteger(screen.total)} matched`;
  return `${formatInteger(screen.total)} matched, ${formatInteger(screen.row_count)} kept`;
}

function Screens({ symbol }: { symbol: string | null }) {
  const go = usePanelRun();
  const { state, retry } = usePanelData<ScreenSummary[]>(
    "screens",
    getScreens,
    (rows) => rows.length === 0,
  );
  const screens = state.kind === LoadState.Ready ? state.data : EMPTY_SCREENS;
  const open = (index: number) => {
    const screen = screens[index];
    if (screen) {
      go({ symbol, code: "EQS", args: { screen: screen.screen_key } });
    }
  };
  const [selected, setSelected] = useListKeys(screens.length, open);

  if (state.kind === LoadState.Loading) return <p className="muted">Loading the screens…</p>;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) {
    return (
      <section>
        <EmptyCard what="screens" />
        <p className="muted">
          None are enabled. `yfin screens` runs them; the admin page has the switch.
        </p>
      </section>
    );
  }
  if (state.kind !== LoadState.Ready) return null;

  return (
    <section>
      <p className="detail-meta">
        {screens.length} screens. Enter or click opens one.
      </p>
      <ul className="list" role="listbox" aria-label="screens">
        {screens.map((screen, index) => (
          <li
            key={screen.screen_key}
            role="option"
            tabIndex={-1}
            className={index === selected ? "list-row row-selected" : "list-row"}
            aria-selected={index === selected}
            onClick={() => {
              setSelected(index);
              open(index);
            }}
          >
            <span className="ds-name">{screen.screen_key}</span>{" "}
            <span className="muted">
              {screen.quote_type.toLowerCase()} · {screen.kind} · {countLabel(screen)} ·{" "}
              {runLabel(screen)}
            </span>{" "}
            <span>{screen.description ?? screen.title}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

//: Shared empties, so "no rows yet" keeps its identity across renders.
const EMPTY_SCREENS: ScreenSummary[] = [];
const EMPTY_ROWS: ScreenRow[] = [];

/** A percentage the screener sends as a plain number: 25 means 25%. */
function percent(value: string | null): string {
  return value === null ? "—" : `${formatDecimal(value)}%`;
}

/** The tab row, and the screen's own header above it.
 *
 *  Drawn from whichever tab loaded the screen, so switching tabs does
 *  not blank the line that says which screen this is. */
function ScreenHead(props: {
  name: string;
  tab: ScreenTab;
  screen: ScreenSummary | null;
  symbol: string | null;
}): ReactElement {
  const go = usePanelRun();
  const { name, tab, screen, symbol } = props;
  return (
    <>
      <p className="detail-meta">
        <span className="ds-name">{screen?.title ?? name}</span>
        {screen !== null && (
          <>
            {" "}
            · {countLabel(screen)} · {runLabel(screen)} · sorted by {screen.sort_field}{" "}
            {screen.sort_asc ? "ascending" : "descending"}
            {screen.description !== null && <> — {screen.description}</>}
          </>
        )}
      </p>
      <div className="tabs" role="tablist" aria-label="screen">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={key === tab}
            className={key === tab ? "tab tab-active" : "tab"}
            onClick={() =>
              go({
                symbol,
                code: "EQS",
                args: key === ScreenTab.Members ? { screen: name } : { screen: name, tab: key },
              })
            }
          >
            {label}
          </button>
        ))}
      </div>
    </>
  );
}

/** This screen's run history, through the generic dataset surface.
 *
 *  `screen_runs` is already a catalogue entry filtered by `screen_key`,
 *  so the typed table draws every column of it -- the roster's size over
 *  time, the page count, and Yahoo's echoed criteria. A hand-written
 *  route here would be a second way to read one table. */
function Runs({ name, symbol }: { name: string; symbol: string | null }): ReactElement {
  return (
    <DatasetView
      name="screen_runs"
      symbol={symbol}
      filters={{ screen_key: name }}
      mode={SymbolMode.None}
    />
  );
}

function Roster({ name, symbol }: { name: string; symbol: string | null }) {
  const go = usePanelRun();
  const [offset, setOffset] = useState(0);
  const { state, retry } = usePanelData<ScreenDetail>(
    `${name}|${offset}`,
    () => getScreen(name, offset),
  );
  const detail = state.kind === LoadState.Ready ? state.data : null;
  const { rows, sort, toggle: sortBy } = useSortedRows(detail?.rows ?? EMPTY_ROWS);

  const open = (index: number) => {
    const row = rows[index];
    // A screener exists to be walked into: Enter opens the symbol's
    // description, which is where every other panel is one key away.
    if (row) go({ symbol: row.symbol, code: "DES", args: {} });
  };
  const [selected, setSelected] = useListKeys(rows.length, open);
  // One request for the page of the roster on screen. A roster can hold
  // a thousand members; only the 250 of this page are asked for, and the
  // hook cuts at the route's cap above that.
  const spark = useSparklines(rows.map((row) => row.symbol));

  const columns = useMemo<Column<ScreenRow>[]>(
    () => [
      { key: "rank_index", label: "#", align: "right", format: (r) => String(r.rank_index + 1) },
      { key: "symbol", label: "Symbol" },
      { key: "short_name", label: "Name", format: (r) => r.short_name ?? "—" },
      { key: "price", label: "Price", align: "right", format: (r) => formatDecimal(r.price) },
      {
        key: "change",
        label: "Change",
        align: "right",
        format: (r) => formatDecimal(r.change),
      },
      {
        key: "change_percent",
        label: "%",
        align: "right",
        format: (r) => percent(r.change_percent),
      },
      {
        key: "volume",
        label: "Volume",
        align: "right",
        format: (r) => (r.volume === null ? "—" : formatInteger(r.volume)),
      },
      {
        key: "market_cap",
        label: "Market cap",
        align: "right",
        format: (r) => formatDecimal(r.market_cap),
      },
      {
        key: "trailing_pe",
        label: "P/E",
        align: "right",
        format: (r) => formatDecimal(r.trailing_pe),
      },
      {
        key: "fifty_two_week_change_percent",
        label: "52w %",
        align: "right",
        format: (r) => percent(r.fifty_two_week_change_percent),
      },
      { key: "exchange", label: "Exchange", format: (r) => r.exchange ?? "—" },
      // Last, unlike `WLA`'s: this grid is eleven columns wide and the
      // shape belongs beside the rank rather than in the middle of the
      // quote.
      {
        key: "spark",
        label: sparkLabel(spark),
        format: (r) => <SparkCell data={spark} symbol={r.symbol} />,
      },
    ],
    [spark],
  );

  if (state.kind === LoadState.Loading) return <p className="muted">Loading {name}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={name} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (detail === null) return null;

  const first = detail.offset + 1;
  const last = detail.offset + rows.length;
  return (
    <section>
      <ScreenHead name={name} tab={ScreenTab.Members} screen={detail.screen} symbol={symbol} />
      {rows.length === 0 ? (
        <EmptyCard what={`rows for ${name}`} />
      ) : (
        <div className="scroll-x">
          <DataTable
            columns={columns}
            rows={rows}
            sort={sort}
            onSort={sortBy}
            rowKey={(row) => row.symbol}
            selected={selected}
            onSelect={(index) => {
              setSelected(index);
              open(index);
            }}
          />
        </div>
      )}
      {(detail.offset > 0 || detail.truncated) && (
        <p className="load-more">
          <button
            type="button"
            disabled={detail.offset === 0}
            onClick={() => setOffset(Math.max(0, detail.offset - SCREEN_PAGE))}
          >
            Previous
          </button>{" "}
          <button
            type="button"
            disabled={!detail.truncated}
            onClick={() => setOffset(detail.offset + SCREEN_PAGE)}
          >
            Next
          </button>{" "}
          <span className="muted">
            {first}–{last}
            {detail.screen.row_count === null ? "" : ` of ${formatInteger(detail.screen.row_count)}`}
          </span>
        </p>
      )}
      <p className="chart-legend">
        <span className="muted">
          Enter opens the selected symbol. Every column of the quote snapshot is in{" "}
          <code className="usage">DS screen_quotes</code>.
        </span>
        {rows.some((row) => !row.is_known) && (
          <span className="muted">
            Rows without a price are symbols outside this deployment&apos;s universe; the screen
            matched them all the same.
          </span>
        )}
      </p>
    </section>
  );
}

export function EQS({ symbol, args }: PanelProps) {
  const name = args.screen;
  if (name === undefined) return <Screens symbol={symbol} />;
  // Args reach a panel from a hand-edited URL too, so an unknown tab
  // falls back to the roster rather than rendering nothing.
  if (args.tab === ScreenTab.Runs) {
    return (
      <section>
        <ScreenHead name={name} tab={ScreenTab.Runs} screen={null} symbol={symbol} />
        <Runs name={name} symbol={symbol} />
      </section>
    );
  }
  return <Roster name={name} symbol={symbol} />;
}

export const EQS_PANEL: PanelSpec = {
  code: "EQS",
  title: "Equity screening: every screen and what it matched",
  usage: "EQS [screen]",
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs,
  component: EQS,
};
