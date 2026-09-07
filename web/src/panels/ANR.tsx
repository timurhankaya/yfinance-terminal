import { getDataset } from "../api/client";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import { DataTable, EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData, type Column } from "./common";
import { asNumber, text } from "./format";

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

function SectionView(props: {
  title: string;
  what: string;
  section: Section;
  columns: Column<Row>[];
  pick: (rows: Row[]) => Row[];
  rowKey: (row: Row, index: number) => string;
  onRetry: () => void;
}) {
  const { title, what, section, columns, pick, rowKey, onRetry } = props;
  return (
    <section className="anr-section">
      <h3>{title}</h3>
      {"error" in section ? (
        <ErrorCard message={section.error} onRetry={onRetry} />
      ) : section.rows.length === 0 ? (
        <p className="muted">No {what}.</p>
      ) : (
        <DataTable columns={columns} rows={pick(section.rows)} rowKey={rowKey} />
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
  return (
    <div className="anr">
      <SectionView title="Price targets" what="price targets" section={s.targets} columns={TARGET_COLUMNS}
        pick={(rows) => rows.slice(0, 1)} rowKey={(r) => String(r.as_of_date)} onRetry={retry} />
      <SectionView title="Recommendations" what="recommendations" section={s.recommendations}
        columns={RECOMMENDATION_COLUMNS} pick={(rows) => rows.slice(0, 4)}
        rowKey={(r) => `${r.as_of_date}|${r.period}`} onRetry={retry} />
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
