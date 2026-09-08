import type { ReactNode } from "react";
import { getDataset } from "../api/client";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import {
  EmptyCard,
  ErrorCard,
  LoadState,
  MissingCard,
  SortedTable,
  usePanelData,
  type Column,
} from "./common";
import { asNumber, formatPrice, text } from "./format";
import { Bars, Bullet, type BarSeries } from "./viz";

type Row = Record<string, unknown>;
type Section = { rows: Row[] } | { error: string };

interface Sections {
  targets: Section;
  recommendations: Section;
  grades: Section;
  estimates: Section;
  trend: Section;
}

const SOURCES: Array<[key: keyof Sections, dataset: string]> = [
  ["targets", "analyst_price_targets"],
  ["recommendations", "recommendations"],
  ["grades", "upgrades_downgrades"],
  ["estimates", "earnings_estimate"],
  ["trend", "eps_trend"],
];

function sectionOf(result: PromiseSettledResult<Row[]> | undefined): Section {
  if (result === undefined) return { error: "missing" };
  if (result.status === "rejected") {
    const reason: unknown = result.reason;
    return { error: reason instanceof Error ? reason.message : String(reason) };
  }
  return { rows: result.value };
}

async function loadSections(symbol: string): Promise<Sections> {
  const settled = await Promise.allSettled(SOURCES.map(([, dataset]) => getDataset(dataset, symbol)));
  // Named one by one rather than built into an empty object and cast:
  // the five fields are the type, and a loop over them can only be typed
  // by asserting the result is complete before it is.
  return {
    targets: sectionOf(settled[0]),
    recommendations: sectionOf(settled[1]),
    grades: sectionOf(settled[2]),
    estimates: sectionOf(settled[3]),
    trend: sectionOf(settled[4]),
  };
}

function allEmpty(sections: Sections): boolean {
  return Object.values(sections).every((s) => "rows" in s && s.rows.length === 0);
}

function num(row: Row, key: string): string {
  const n = asNumber(row[key]);
  return n === null ? "—" : n.toFixed(2);
}

function newestOnly(rows: Row[]): Row[] {
  const dates = rows.map((r) => String(r.as_of_date ?? "")).sort();
  const newest = dates[dates.length - 1];
  return newest === undefined ? rows : rows.filter((r) => String(r.as_of_date ?? "") === newest);
}

const TARGET_COLUMNS: Column<Row>[] = ["current", "low", "mean", "median", "high"].map((key) => ({
  key, label: key.charAt(0).toUpperCase() + key.slice(1), align: "right", format: (row: Row) => num(row, key),
}));

const RECOMMENDATION_COLUMNS: Column<Row>[] = [
  { key: "as_of_date", label: "As of", format: (r) => text(r, "as_of_date") },
  { key: "period", label: "Period", format: (r) => text(r, "period") },
  ...["strong_buy", "buy", "hold", "sell", "strong_sell"].map((key) => ({
    key,
    label: key.split("_").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" "),
    align: "right" as const,
    format: (r: Row) => text(r, key),
  })),
];

const GRADE_COLUMNS: Column<Row>[] = [
  { key: "grade_ts_utc", label: "When", format: (r) => text(r, "grade_ts_utc").slice(0, 10) },
  { key: "firm", label: "Firm", format: (r) => text(r, "firm") },
  { key: "grade", label: "Grade", format: (r) => `${text(r, "from_grade")} → ${text(r, "to_grade")}` },
  { key: "action", label: "Action", format: (r) => text(r, "action") },
];

const ESTIMATE_COLUMNS: Column<Row>[] = [
  { key: "period", label: "Period", format: (r) => text(r, "period") },
  { key: "avg", label: "Avg", align: "right", format: (r) => num(r, "avg") },
  { key: "low", label: "Low", align: "right", format: (r) => num(r, "low") },
  { key: "high", label: "High", align: "right", format: (r) => num(r, "high") },
  { key: "number_of_analysts", label: "Analysts", align: "right", format: (r) => text(r, "number_of_analysts") },
  { key: "growth", label: "Growth", align: "right", format: (r) => num(r, "growth") },
];

const TREND_COLUMNS: Column<Row>[] = [
  { key: "period", label: "Period", format: (r) => text(r, "period") },
  ...["current", "days_ago_7", "days_ago_30", "days_ago_60", "days_ago_90"].map((key) => ({
    key, label: key.replace("days_ago_", "-").replace("current", "now"), align: "right" as const,
    format: (r: Row) => num(r, key),
  })),
];


// --- the two pictures --------------------------------------------------------
//
// Both sit ABOVE their table rather than instead of it (spec,
// "Kararlar" 3): a chart's one weakness is the exact figure, and an
// analyst's exact figure is the point of the section.

export interface TargetRange {
  low: number;
  high: number;
  mean: number;
  /** Where the price actually is, or null when the snapshot has none. */
  actual: number | null;
}

/** The newest price-target row as a range with a mark on it.
 *
 *  Null unless low, mean and high are all there and in order: three
 *  numbers about one thing is what makes the picture, and a range drawn
 *  from two of them would be a different claim. */
export function targetRange(rows: Row[]): TargetRange | null {
  const newest = rows[0];
  if (newest === undefined) return null;
  const low = asNumber(newest.low);
  const high = asNumber(newest.high);
  const mean = asNumber(newest.mean);
  if (low === null || high === null || mean === null || high < low) return null;
  return { low, high, mean, actual: asNumber(newest.current) };
}

//: The five buckets Yahoo counts analysts into, strongest first -- which
//: is also the order they read in on the axis.
const RATINGS: Array<[key: string, label: string]> = [
  ["strong_buy", "Strong buy"],
  ["buy", "Buy"],
  ["hold", "Hold"],
  ["sell", "Sell"],
  ["strong_sell", "Strong sell"],
];

export interface RecommendationBars {
  categories: string[];
  series: BarSeries[];
}

/** The newest snapshot's recommendation counts, one group per period.
 *
 *  Three or four periods, because that is what the source carries (`0m`
 *  back to `-3m`) -- the chart has exactly as many groups as the table
 *  has rows. Oldest first: the axis is time, and the interesting thing
 *  is which way the counts moved. */
export function recommendationBars(rows: Row[]): RecommendationBars | null {
  const snapshot = newestOnly(rows);
  const ordered = [...snapshot].sort((a, b) => monthsAgo(a) - monthsAgo(b));
  if (ordered.length === 0) return null;
  const series: BarSeries[] = RATINGS.map(([key, label]) => ({
    key,
    label,
    values: ordered.map((row) => asNumber(row[key])),
  }));
  // Every count missing is not a distribution; the table still shows
  // whatever the row did carry.
  if (series.every((one) => one.values.every((value) => value === null))) return null;
  return { categories: ordered.map((row) => String(row.period ?? "?")), series };
}

/** `-3m` -> -3, `0m` -> 0. A relative period key, sorted as the number
 *  it is: sorted as text, `-1m` would come before `0m` but after
 *  `-3m` only by luck. */
function monthsAgo(row: Row): number {
  const parsed = Number.parseInt(String(row.period ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function SectionView(props: {
  title: string;
  what: string;
  section: Section;
  columns: Column<Row>[];
  pick: (rows: Row[]) => Row[];
  rowKey: (row: Row, index: number) => string;
  onRetry: () => void;
  /** Drawn above the table when the section has one. */
  chart?: ReactNode;
}) {
  const { title, what, section, columns, pick, rowKey, onRetry, chart = null } = props;
  return (
    <section className="anr-section">
      <h3>{title}</h3>
      {"error" in section ? (
        <ErrorCard message={section.error} onRetry={onRetry} />
      ) : section.rows.length === 0 ? (
        <p className="muted">No {what}.</p>
      ) : (
        <>
          {chart}
          <SortedTable columns={columns} rows={pick(section.rows)} rowKey={rowKey} />
        </>
      )}
    </section>
  );
}

export function ANR({ symbol }: PanelProps) {
  const { state, retry } = usePanelData<Sections>(
    symbol ?? "",
    () => (symbol === null ? Promise.reject(new Error("no symbol")) : loadSections(symbol)),
    allEmpty,
  );
  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <EmptyCard what="analyst data" />;

  const s = state.data;
  const range = "rows" in s.targets ? targetRange(s.targets.rows) : null;
  const distribution = "rows" in s.recommendations ? recommendationBars(s.recommendations.rows) : null;
  return (
    <div className="anr">
      <SectionView title="Price targets" what="price targets" section={s.targets} columns={TARGET_COLUMNS}
        pick={(rows) => rows.slice(0, 1)} rowKey={(r) => String(r.as_of_date)} onRetry={retry}
        chart={
          range === null ? null : (
            <Bullet
              label={`${symbol} price target: ${formatPrice(range.low)} to ${formatPrice(range.high)}, mean ${formatPrice(range.mean)}`}
              low={range.low}
              high={range.high}
              mean={range.mean}
              actual={range.actual}
              format={formatPrice}
            />
          )
        } />
      <SectionView title="Recommendations" what="recommendations" section={s.recommendations}
        columns={RECOMMENDATION_COLUMNS} pick={(rows) => rows.slice(0, 4)}
        rowKey={(r) => `${r.as_of_date}|${r.period}`} onRetry={retry}
        chart={
          distribution === null ? null : (
            <Bars
              label={`${symbol} recommendations by period`}
              categories={distribution.categories}
              series={distribution.series}
              format={(value) => String(Math.round(value))}
            />
          )
        } />
      <SectionView title="Upgrades and downgrades" what="grade changes" section={s.grades} columns={GRADE_COLUMNS}
        pick={(rows) => rows.slice(0, 20)} rowKey={(r) => `${r.grade_ts_utc}|${r.firm}`} onRetry={retry} />
      <SectionView title="Earnings estimate (EPS)" what="earnings estimates" section={s.estimates}
        columns={ESTIMATE_COLUMNS} pick={newestOnly} rowKey={(r) => `${r.as_of_date}|${r.period}`} onRetry={retry} />
      <SectionView title="EPS trend" what="EPS trend" section={s.trend} columns={TREND_COLUMNS}
        pick={newestOnly} rowKey={(r) => `${r.as_of_date}|${r.period}`} onRetry={retry} />
    </div>
  );
}

export const ANR_PANEL: PanelSpec = {
  code: "ANR",
  title: "Analyst ratings",
  needsSymbol: true,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: ANR,
};
