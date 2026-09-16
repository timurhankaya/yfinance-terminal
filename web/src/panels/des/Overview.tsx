// The first screen of a symbol: who it is, six numbers, three pictures.
// Everything except the trend line comes out of the SAME `info` object
// the panel already loaded, so the pictures cost no request; the trend
// line costs one.
import type { ReactElement, ReactNode } from "react";
import { daysAgo, getBarsWindow, type SymbolDetail } from "../../api/client";
import { LoadState, usePanelData } from "../common";
import { formatPrice } from "../format";
import { quoteUrl } from "../links";
import { Bars, Bullet, Sparkline } from "../viz";
import { formatInfo } from "./info";
import {
  TREND_RANGES,
  TREND_WINDOW,
  TrendRange,
  analystRange,
  closesOf,
  marginBars,
  statDirection,
  statsOf,
  yearRange,
  type RangeMark,
} from "./layout";

/** One fact about the symbol's identity, as a chip. Absent facts are not
 *  drawn: a row of "—" says nothing a missing chip does not. */
function Chip({ children }: { children: ReactNode }): ReactElement | null {
  if (children === null || children === undefined || children === "") return null;
  return <span className="des-chip">{children}</span>;
}

function Card({ title, actions, children, className = "" }: {
  className?: string;
  title: string;
  /** The card's own controls, on the title's line. A range picker under
   *  the chart would push the chart down; beside the title it costs no
   *  vertical space at all. */
  actions?: ReactNode;
  children: ReactNode;
}): ReactElement {
  return (
    <div className={`viz-card ${className}`}>
      <div className="viz-card-head">
        <h4>{title}</h4>
        {actions}
      </div>
      {children}
    </div>
  );
}

function RangeCard({ title, range }: { title: string; range: RangeMark | null }): ReactElement | null {
  if (range === null) return null;
  return (
    <Card title={title}>
      <Bullet
        low={range.low}
        high={range.high}
        mean={range.mean}
        actual={range.actual}
        label={range.label}
        format={formatPrice}
      />
    </Card>
  );
}

/** The trend, over the window the reader picked. Bars rather than the
 *  batched sparkline route: that one is daily closes only and refuses an
 *  interval (`ui/data.py`), and two of the four windows are intraday. */
function TrendCard({ symbol, range, onRange }: {
  symbol: string;
  range: TrendRange;
  onRange: (range: TrendRange) => void;
}): ReactElement {
  const window = TREND_WINDOW[range];
  const { state } = usePanelData<number[]>(
    `trend|${symbol}|${range}`,
    async () => closesOf(await getBarsWindow(symbol, window.interval, daysAgo(window.days)), window.session),
  );
  const picker = (
    <span className="range" role="group" aria-label="trend window">
      {TREND_RANGES.map((option) => (
        <button
          key={option}
          type="button"
          className={option === range ? "chip chip-on" : "chip"}
          aria-pressed={option === range}
          data-tooltip={`Trend window · ${TREND_WINDOW[option].label}`}
          onClick={() => onRange(option)}
        >
          {option.toUpperCase()}
        </button>
      ))}
    </span>
  );

  const closes = state.kind === LoadState.Ready ? state.data : [];
  const first = closes[0];
  const last = closes[closes.length - 1];
  const move = first === undefined || last === undefined || first === last
    ? undefined
    : last > first
      ? "up"
      : "down";
  return (
    <Card className="viz-card-trend" title={window.label} actions={picker}>
      {state.kind === LoadState.Loading && <p className="muted des-trend-note">Loading…</p>}
      {state.kind === LoadState.Error && <p className="muted des-trend-note">No bars for this window.</p>}
      {state.kind !== LoadState.Loading && state.kind !== LoadState.Error && closes.length < 2 && (
        // Said, not left blank: the archive having no bars for a window
        // is a fact about the archive, and a card that quietly vanished
        // would look like a bug in the page.
        <p className="muted des-trend-note">No bars for this window.</p>
      )}
      {closes.length >= 2 && (
        <>
          <div className="des-trend">
            <Sparkline
              area
              values={closes}
              label={`${symbol}, ${window.label.toLowerCase()}, ${closes.length} bars, ${move ?? "flat"}`}
            />
          </div>
          <p className={move === undefined ? "des-trend-note muted" : `des-trend-note ${move}`}>
            {formatPrice(first)} → {formatPrice(last)}
            <span className="muted"> · {closes.length} bars</span>
          </p>
        </>
      )}
    </Card>
  );
}

export function Overview({ detail, range, onRange, onOpen }: {
  onOpen: (code: string) => void;
  detail: SymbolDetail;
  range: TrendRange;
  onRange: (range: TrendRange) => void;
}): ReactElement {
  const info = detail.info ?? {};
  const stats = statsOf(info);
  const margins = marginBars(info);
  const summary = typeof info.long_business_summary === "string" ? info.long_business_summary : null;

  return (
    <>
      <div className="des-head">
        <div className="des-identity"><p className="des-eyebrow">Security overview <span>/ Reference snapshot</span></p><h2 className="des-name">{detail.long_name ?? detail.short_name ?? detail.symbol}</h2></div>
        <div className="des-meta">
          <Chip>{detail.symbol}</Chip>
          <Chip>{detail.full_exchange_name ?? detail.exchange}</Chip>
          <Chip>{detail.quote_type}</Chip>
          <Chip>{detail.currency}</Chip>
          <Chip>{detail.timezone}</Chip>
          {!detail.is_active && <span className="des-chip warn">inactive</span>}
        </div>
        <a className="des-out" href={quoteUrl(detail.symbol)} target="_blank" rel="noopener noreferrer">
          Yahoo ↗
        </a>
      </div>

      <nav className="des-actions" aria-label="symbol quick links">
        {([["GP", "Price chart"], ["FA", "Financials"], ["HDS", "Ownership"], ["N", "News"]] as const).map(([code, label]) => (
          <button type="button" key={code} onClick={() => onOpen(code)}><span>{code}</span>{label}<span aria-hidden="true">↗</span></button>
        ))}
      </nav>
      {stats.length > 0 && (
        <div className="stats">
          {stats.map((stat) => {
            const move = statDirection(stat, info[stat.key]);
            return (
              <div className="stat" key={stat.key}>
                <span className="stat-label" title={stat.key}>{stat.label}</span>
                <span className={move === undefined ? "stat-value" : `stat-value ${move}`}>
                  {formatInfo(stat.key, info[stat.key])}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <div className="des-viz">
        <TrendCard symbol={detail.symbol} range={range} onRange={onRange} />
        <RangeCard title="52-week range" range={yearRange(info)} />
        <RangeCard title="Analyst targets" range={analystRange(info)} />
        {margins !== null && (
          <Card title="Margins">
            <Bars
              label={`${detail.symbol}: margins down the income statement`}
              categories={margins.categories}
              series={[{ key: "margin", label: "% of revenue", values: margins.percents }]}
              format={(value) => `${value.toFixed(0)}%`}
            />
          </Card>
        )}

        {summary !== null && (
          <div className="des-about">
            <h4>Company profile</h4>
            <p>{summary}</p>
          </div>
        )}
      </div>
    </>
  );
}
