// GP: the chart. One code, every interval the archive keeps; the
// interval is an argument like any other. The drawing is two bodies
// (`chart-daily.tsx`, `chart-intraday.tsx`): two tables, two live
// buckets, corporate actions against `bar_gaps`. One function with a
// flag would add branches, not remove them.
import type { ReactElement } from "react";
import { BAR_INTERVALS, Interval, isInterval } from "../api/client";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { Layout } from "../commands/types";
import { DailyChart, SESSION_INTERVALS } from "./chart-daily";
import { INTRADAY_INTERVALS, IntradayChart } from "./chart-intraday";
import { Choice, Controls, NumberArg, useArgs } from "./controls";

const DEFAULT_INTERVAL = Interval.D1;
const DEFAULT_YEARS = 2;
//: The API's own ceiling for a session-interval range is ~10 years.
const MAX_YEARS = 10;

export const GP_ARGS = `GP [${BAR_INTERVALS.join("|")}] [years 1-${MAX_YEARS}]`;
export const GP_USAGE = `Usage: ${GP_ARGS}`;

/** Whether an interval is drawn by the intraday body. The two lists are
 *  the whole vocabulary between them, which `charts.test.tsx` asserts. */
export function isIntraday(interval: Interval): boolean {
  return INTRADAY_INTERVALS.includes(interval);
}

function intervalOr(value: string | undefined, fallback: Interval): Interval {
  return value !== undefined && isInterval(value) ? value : fallback;
}

function yearsInRange(years: number): boolean {
  return Number.isInteger(years) && years >= 1 && years <= MAX_YEARS;
}

function parseArgs(tokens: string[]): PanelArgs {
  const [first, second, ...rest] = tokens;
  if (rest.length > 0) throw new Error(GP_USAGE);
  if (first === undefined) return { interval: DEFAULT_INTERVAL, years: String(DEFAULT_YEARS) };
  const interval = first.toLowerCase();
  if (!isInterval(interval)) throw new Error(GP_USAGE);
  if (second === undefined) return { interval, years: String(DEFAULT_YEARS) };
  const years = Number(second);
  // Refused rather than ignored on an intraday interval: `GP 5m 3` reads
  // as three years of five-minute bars, and the archive has no such
  // thing. The window there is fixed at five sessions.
  if (isIntraday(interval)) throw new Error(`${GP_USAGE} — years applies to ${SESSION_INTERVALS.join("/")} only`);
  if (!yearsInRange(years)) throw new Error(GP_USAGE);
  return { interval, years: String(years) };
}

export function GP({ symbol, args }: PanelProps): ReactElement | null {
  const interval = intervalOr(args.interval, DEFAULT_INTERVAL);
  const years = Number(args.years ?? DEFAULT_YEARS);
  const set = useArgs("GP", symbol, args);

  if (symbol === null) return null;
  const intraday = isIntraday(interval);
  // The controls are the panel's, not its ready state's: an interval
  // with no bars is exactly when a reader needs to pick another one.
  return (
    <section>
      <Controls>
        <Choice
          label="Interval"
          value={interval}
          options={BAR_INTERVALS}
          onPick={(next) => set({ interval: next })}
        />
        {!intraday && (
          <NumberArg
            label="Window"
            value={yearsInRange(years) ? years : DEFAULT_YEARS}
            min={1}
            max={MAX_YEARS}
            onSet={(next) => set({ years: String(next) })}
            suffix={years === 1 ? "year" : "years"}
          />
        )}
        <span className="usage">{GP_ARGS}</span>
      </Controls>
      {intraday ? (
        <IntradayChart symbol={symbol} interval={interval} />
      ) : (
        <DailyChart symbol={symbol} interval={interval} years={yearsInRange(years) ? years : DEFAULT_YEARS} />
      )}
    </section>
  );
}

export const GP_PANEL: PanelSpec = {
  code: "GP",
  title: "Candles, at any interval: volume, corporate actions, and the archive's gaps",
  usage: GP_ARGS,
  needsSymbol: true,
  layout: Layout.Headed,
  parseArgs,
  // Args can arrive from a hand-edited URL, not only from parseArgs.
  normalizeArgs: (args) => {
    const interval = intervalOr(args.interval, DEFAULT_INTERVAL);
    const years = Number(args.years ?? DEFAULT_YEARS);
    return { ...args, interval, years: String(yearsInRange(years) ? years : DEFAULT_YEARS) };
  },
  component: GP,
};
