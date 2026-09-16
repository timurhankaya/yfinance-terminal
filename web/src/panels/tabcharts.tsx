// The charts a curated tab can declare. `HDS` and its siblings are
// configuration rather than code: a tab names the chart it wants and
// `DatasetView` draws it from the rows it already loaded, so no chart
// fetches anything of its own. Every one sits ABOVE its table, never
// instead of it.
import type { ReactElement } from "react";
import type { Row } from "../api/client";
import { asNumber, formatBig } from "./format";
import { Bars } from "./viz";

/** Which chart a tab wants. A tab that names none gets its table alone. */
export enum TabChart {
  InstitutionalShare = "institutional_share",
  InsiderFlow = "insider_flow",
}

//: Six holders, not ten. The axis is 528 points wide, and a name has to
//: stay legible under its bar; the table below carries every holder the
//: archive has, in full.
export const TOP_HOLDERS = 6;
const NAME_CHARS = 12;

export interface HolderBars {
  holders: string[];
  /** Percent of shares outstanding, as a percentage rather than the
   *  fraction the archive stores (0.0165 -> 1.65). */
  percents: Array<number | null>;
}

/** The largest institutional holders by share of the company.
 *
 *  By `pct_held` rather than by value: the question a holder list
 *  answers is how much of the company one institution controls, and a
 *  dollar value makes a large holder of a small company look small. */
export function holderBars(rows: Row[], top: number = TOP_HOLDERS): HolderBars | null {
  const held = rows.flatMap((row) => {
    const percent = asNumber(row.pct_held);
    const holder = typeof row.holder === "string" ? row.holder : null;
    return percent === null || holder === null ? [] : [{ holder, percent: percent * 100 }];
  });
  if (held.length === 0) return null;
  const largest = held.sort((a, b) => b.percent - a.percent).slice(0, top);
  return {
    // Truncated on the axis, in full in the table: a name wider than its
    // bar overlaps the one beside it and both become unreadable.
    holders: largest.map((one) =>
      one.holder.length > NAME_CHARS ? `${one.holder.slice(0, NAME_CHARS - 1)}…` : one.holder,
    ),
    percents: largest.map((one) => one.percent),
  };
}

export interface InsiderFlow {
  categories: string[];
  shares: Array<number | null>;
  /** The window the source aggregated over, e.g. `6m`. */
  period: string;
}

/** The newest insider-activity snapshot: bought, sold, and the net.
 *  `insider_activity`, not `insider_transactions`: the transaction table
 *  carries direction only inside free text, while this one is Yahoo's
 *  own aggregate with the share counts outright. */
export function insiderFlow(rows: Row[]): InsiderFlow | null {
  const newest = rows[0];
  if (newest === undefined) return null;
  const shares = [
    asNumber(newest.purchases_shares),
    asNumber(newest.sales_shares),
    asNumber(newest.net_shares),
  ];
  if (shares.every((value) => value === null)) return null;
  return {
    categories: ["Bought", "Sold", "Net"],
    shares,
    period: typeof newest.period_label === "string" ? newest.period_label : "",
  };
}

/** The chart a tab declared, drawn from the rows the view already has.
 *
 *  Null whenever the rows cannot answer it: a tab whose dataset came
 *  back without the columns this needs still shows its table. */
export function TabChartView(props: {
  kind: TabChart;
  rows: Row[];
  symbol: string | null;
}): ReactElement | null {
  const { kind, rows, symbol } = props;
  const who = symbol ?? "this symbol";
  if (kind === TabChart.InstitutionalShare) {
    const bars = holderBars(rows);
    if (bars === null) return null;
    return (
      <Bars
        label={`${who}: the ${bars.holders.length} largest institutional holders`}
        categories={bars.holders}
        series={[{ key: "pct_held", label: "% of shares held", values: bars.percents }]}
        format={(value) => `${value.toFixed(1)}%`}
      />
    );
  }
  const flow = insiderFlow(rows);
  if (flow === null) return null;
  return (
    <Bars
      label={`${who}: insider shares bought and sold${flow.period === "" ? "" : ` over ${flow.period}`}`}
      categories={flow.categories}
      series={[
        {
          key: "shares",
          label: flow.period === "" ? "Shares" : `Shares, last ${flow.period}`,
          values: flow.shares,
        },
      ]}
      format={formatBig}
    />
  );
}
