// HEAT: the market as area and colour.
//
// Two questions in one picture that a table answers in two columns the
// reader has to hold in their head together -- how big is this, and
// which way did it go. Area is market capitalisation, colour is the move
// over the chosen window.
//
// Two rosters, one shape. Without an argument it draws the eleven
// sectors, joining `domains` (which key is a sector, and what it is
// called) to `domain_metrics` (what it is worth and how it moved). With
// a screen key it draws that screen's members, from the roster route
// `EQS` already reads.
//
// Colour never carries the value on its own: the scale is always drawn
// (spec, "Kararlar" 7), the number is in the box wherever there is room
// for it, and Enter opens the thing itself.
import { useMemo } from "react";
import type { ReactElement } from "react";
import { PAGE_LIMIT, getDatasetPage, getScreen, type Row, type ScreenDetail } from "../api/client";
import { useGo } from "../commands/go";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { ErrorCard, LoadState, usePanelData } from "./common";
import { asNumber, formatBig } from "./format";
import { OTHER_KEY, Treemap, type TreemapItem } from "./viz";

/** The windows a heat map can be coloured by.
 *
 *  Two sources with two different answers: a sector's metrics row
 *  carries five performance figures, a screen's quote snapshot carries
 *  today's move and the 52-week change. The panel offers only what the
 *  source it is drawing actually holds. */
export enum HeatPeriod {
  Day = "1d",
  Ytd = "ytd",
  Year = "1y",
  ThreeYear = "3y",
  FiveYear = "5y",
  Week52 = "52w",
}

//: The column each period reads, per source. A period absent from a map
//: is a period that source cannot answer.
const SECTOR_COLUMN: Partial<Record<HeatPeriod, string>> = {
  [HeatPeriod.Day]: "reg_market_change_pct",
  [HeatPeriod.Ytd]: "ytd_change_pct",
  [HeatPeriod.Year]: "one_year_change_pct",
  [HeatPeriod.ThreeYear]: "three_year_change_pct",
  [HeatPeriod.FiveYear]: "five_year_change_pct",
};

const SCREEN_COLUMN: Partial<Record<HeatPeriod, keyof ScreenMember>> = {
  [HeatPeriod.Day]: "change_percent",
  [HeatPeriod.Week52]: "fifty_two_week_change_percent",
};

type ScreenMember = ScreenDetail["rows"][number];

const PERIOD_LABEL: Record<HeatPeriod, string> = {
  [HeatPeriod.Day]: "today",
  [HeatPeriod.Ytd]: "year to date",
  [HeatPeriod.Year]: "1 year",
  [HeatPeriod.ThreeYear]: "3 years",
  [HeatPeriod.FiveYear]: "5 years",
  [HeatPeriod.Week52]: "52 weeks",
};

//: The change that reaches full colour, per window. Fixed rather than
//: taken from the data: a scale that rescaled itself would make a quiet
//: day look like a crash, and two heat maps could not be compared.
const PERIOD_SPAN: Record<HeatPeriod, number> = {
  [HeatPeriod.Day]: 3,
  [HeatPeriod.Ytd]: 20,
  [HeatPeriod.Year]: 30,
  [HeatPeriod.ThreeYear]: 60,
  [HeatPeriod.FiveYear]: 100,
  [HeatPeriod.Week52]: 30,
};

export const SECTOR_PERIODS = Object.keys(SECTOR_COLUMN) as HeatPeriod[];
export const SCREEN_PERIODS = Object.keys(SCREEN_COLUMN) as HeatPeriod[];

export const HEAT_ARGS = "HEAT [screen] [period]";
export const HEAT_USAGE = `Usage: ${HEAT_ARGS}`;

function isPeriod(token: string): token is HeatPeriod {
  return (Object.values(HeatPeriod) as string[]).includes(token);
}

/** Which periods a view can offer, and the sentence that says why the
 *  others are absent. */
export function periodsFor(screen: string | undefined): HeatPeriod[] {
  return screen === undefined ? SECTOR_PERIODS : SCREEN_PERIODS;
}

function parseArgs(tokens: string[]): PanelArgs {
  if (tokens.length > 2) throw new Error(HEAT_USAGE);
  let screen: string | undefined;
  let period: HeatPeriod | undefined;
  for (const token of tokens) {
    const lower = token.toLowerCase();
    if (isPeriod(lower)) {
      if (period !== undefined) throw new Error(HEAT_USAGE);
      period = lower;
    } else {
      if (screen !== undefined) throw new Error(HEAT_USAGE);
      screen = lower;
    }
  }
  const allowed = periodsFor(screen);
  if (period !== undefined && !allowed.includes(period)) {
    throw new Error(
      `${period} is not available here. ${screen === undefined ? "Sector" : "Screen"} maps offer ${allowed.join(", ")}.`,
    );
  }
  const args: PanelArgs = { period: period ?? allowed[0] ?? HeatPeriod.Day };
  if (screen !== undefined) args.screen = screen;
  return args;
}

/** The newest metrics row per domain, joined to the sector taxonomy.
 *
 *  `domain_metrics` is as-of and sorted newest first, so the first row
 *  seen for a key is that domain's latest -- which is right even when
 *  the sectors were not all fetched on the same day. */
async function loadSectors(period: HeatPeriod): Promise<TreemapItem[]> {
  const column = SECTOR_COLUMN[period];
  if (column === undefined) throw new Error(`${period} is not a sector window`);
  const [taxonomy, metrics] = await Promise.all([
    getDatasetPage("domains", { domain_type: "sector" }, null, PAGE_LIMIT),
    getDatasetPage("domain_metrics", {}, null, PAGE_LIMIT),
  ]);
  const names = new Map<string, string>();
  for (const row of taxonomy.rows) {
    const key = row.domain_key;
    if (typeof key === "string") names.set(key, typeof row.name === "string" ? row.name : key);
  }
  const newest = new Map<string, Row>();
  for (const row of metrics.rows) {
    const key = row.domain_key;
    if (typeof key !== "string" || !names.has(key) || newest.has(key)) continue;
    newest.set(key, row);
  }
  const cells: TreemapItem[] = [];
  for (const [key, row] of newest) {
    const value = asNumber(row.market_cap);
    if (value === null || value <= 0) continue;
    cells.push({
      key,
      label: names.get(key) ?? key,
      value,
      percent: asNumber(row[column]),
    });
  }
  return cells;
}

async function loadScreen(key: string, period: HeatPeriod): Promise<TreemapItem[]> {
  const column = SCREEN_COLUMN[period];
  if (column === undefined) throw new Error(`${period} is not a screen window`);
  const detail = await getScreen(key);
  const cells: TreemapItem[] = [];
  for (const row of detail.rows) {
    const value = asNumber(row.market_cap);
    if (value === null || value <= 0) continue;
    cells.push({
      key: row.symbol,
      label: row.symbol,
      value,
      percent: asNumber(row[column]),
    });
  }
  return cells;
}

export function HEAT({ symbol, args }: PanelProps): ReactElement {
  const go = useGo();
  const screen = args.screen;
  const allowed = periodsFor(screen);
  // A hand-edited URL reaches here unparsed; an unsupported window falls
  // back to the first one this source can answer rather than drawing an
  // all-neutral map.
  const period =
    args.period !== undefined && isPeriod(args.period) && allowed.includes(args.period)
      ? args.period
      : (allowed[0] ?? HeatPeriod.Day);

  const { state, retry } = usePanelData<TreemapItem[]>(
    `${screen ?? ""}|${period}`,
    () => (screen === undefined ? loadSectors(period) : loadScreen(screen, period)),
    (cells) => cells.length === 0,
  );
  const cells = state.kind === LoadState.Ready ? state.data : EMPTY_CELLS;

  const open = useMemo(
    () => (key: string) => {
      // The overflow box is not one thing, so there is nothing to open.
      if (key === OTHER_KEY) return;
      if (screen === undefined) go({ symbol, code: "DOM", args: { tab: "metrics", domain_key: key } });
      else go({ symbol: key, code: "DES", args: {} });
    },
    [go, screen, symbol],
  );

  const title = screen === undefined ? "sectors" : screen;

  if (state.kind === LoadState.Loading) return <p className="muted">Loading {title}…</p>;
  if (state.kind === LoadState.Missing) {
    return (
      <section>
        <p className="card card-error">No such screen: {screen}</p>
        <p className="muted">
          <code className="usage">EQS</code> lists every screen this deployment runs.
        </p>
      </section>
    );
  }
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) {
    return (
      <section>
        <Periods screen={screen} current={period} symbol={symbol} />
        <p className="card card-empty">
          Nothing to draw: no {title} row carries a market capitalisation.
        </p>
      </section>
    );
  }

  const coloured = cells.filter((cell) => cell.percent !== null).length;
  return (
    <section>
      <Periods screen={screen} current={period} symbol={symbol} />
      <p className="chart-note">
        <span>
          {cells.length} {cells.length === 1 ? "box" : "boxes"} · {PERIOD_LABEL[period]} ·{" "}
          {screen === undefined ? "sectors" : `screen ${screen}`}
        </span>
        <span className="muted">
          {screen === undefined
            ? "area: market capitalisation of the sector"
            : "area: market capitalisation of the member"}
        </span>
      </p>
      <Treemap
        items={cells}
        label={`${title}, ${PERIOD_LABEL[period]}`}
        span={PERIOD_SPAN[period]}
        onOpen={open}
        format={(cell) =>
          cell.percent === null
            ? formatBig(cell.value)
            : `${formatBig(cell.value)} · ${cell.percent > 0 ? "+" : ""}${cell.percent.toFixed(1)}%`
        }
      />
      <p className="chart-legend">
        <span className="muted">
          Enter or click a box opens{" "}
          {screen === undefined ? "that sector's metrics (DOM)" : "that symbol (DES)"}.
        </span>
        {coloured < cells.length && (
          <span className="muted">
            {cells.length - coloured} of {cells.length} have no move for this window and are drawn
            neutral.
          </span>
        )}
      </p>
    </section>
  );
}

const EMPTY_CELLS: TreemapItem[] = [];

/** The windows this source can answer, and a line saying why it is not
 *  the same list on both. */
function Periods(props: {
  screen: string | undefined;
  current: HeatPeriod;
  symbol: string | null;
}): ReactElement {
  const { screen, current, symbol } = props;
  const go = useGo();
  const allowed = periodsFor(screen);
  return (
    <>
      <div className="tabs" role="tablist" aria-label="window">
        {allowed.map((period) => (
          <button
            key={period}
            type="button"
            role="tab"
            aria-selected={period === current}
            className={period === current ? "tab tab-active" : "tab"}
            onClick={() =>
              go({
                symbol,
                code: "HEAT",
                args: screen === undefined ? { period } : { screen, period },
              })
            }
          >
            {PERIOD_LABEL[period]}
          </button>
        ))}
      </div>
      <p className="detail-meta">
        {screen === undefined
          ? "A sector's metrics row carries these five windows and no others."
          : "A screen's quote snapshot carries today's move and the 52-week change; the longer windows are a sector map's."}
      </p>
    </>
  );
}

export const HEAT_PANEL: PanelSpec = {
  code: "HEAT",
  title: "Heat map: sectors, or a screen's members, by size and move",
  usage: HEAT_ARGS,
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs,
  normalizeArgs: (args) => {
    const screen = args.screen;
    const allowed = periodsFor(screen);
    const period =
      args.period !== undefined && isPeriod(args.period) && allowed.includes(args.period)
        ? args.period
        : (allowed[0] ?? HeatPeriod.Day);
    const normalized: PanelArgs = { period };
    if (screen !== undefined) normalized.screen = screen;
    return normalized;
  },
  component: HEAT,
};
