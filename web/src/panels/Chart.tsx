// The one place `lightweight-charts` is touched; `chart-data.ts` holds
// everything decided rather than drawn. Two panes, not two price scales:
// volume on the price scale flattens the candles. The gap band IS an
// overlay on the price pane because it has no values of its own. The
// library styles through the CSSOM, which `style-src 'self'` allows.
import { useEffect, useRef } from "react";
import type { ReactElement } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";
import type {
  IChartApi,
  IPriceLine,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import { MarkerKind } from "./chart-data";
import { LOCALE } from "./format";
import { useVizTheme, vizTheme } from "./viz";
import type { ActionMarker, Candle, LinePoint, VolumeBar, Whitespace } from "./chart-data";

export interface ChartProps {
  candles: Candle[];
  volume: VolumeBar[];
  markers: ActionMarker[];
  /** Empty slots so the time scale has room where the archive has none. */
  whitespace: Whitespace[];
  /** The same instants, shaded full height. */
  band: Whitespace[];
  /** Intraday charts show the clock on the axis; a daily one shows dates. */
  timeVisible: boolean;
  /** What series this IS -- symbol, interval, window. The time scale is
   *  fitted once per identity and never again: a live tick must not throw
   *  away the reader's pan and zoom. */
  fitKey?: string;
  label: string;
}

const MARKER_SHAPE: Record<MarkerKind, SeriesMarker<Time>["shape"]> = {
  [MarkerKind.Dividend]: "circle",
  [MarkerKind.Split]: "square",
  [MarkerKind.CapitalGain]: "arrowUp",
};

function stamp<T extends { time: number }>(rows: T[]): Array<T & { time: UTCTimestamp }> {
  return rows as Array<T & { time: UTCTimestamp }>;
}

export function Chart(props: ChartProps): ReactElement {
  const theme = useVizTheme();
  const { candles, volume, markers, whitespace, band, timeVisible, fitKey, label } = props;
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const priceSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeries = useRef<ISeriesApi<"Histogram"> | null>(null);
  const bandSeries = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markerApi = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // Built once. Rebuilding it per data change would lose the reader's pan
  // and zoom on every live tick.
  useEffect(() => {
    const element = host.current;
    if (element === null) return;
    const colors = vizTheme();
    const instance = createChart(element, {
      // ResizeObserver, so the chart follows the panel without a listener
      // of ours; width/height are the fallback if it is unavailable.
      autoSize: true,
      // A fixed locale, like every other number in the terminal: the axis
      // must not read 1.234,56 on a tr-TR machine and 1,234.56 here.
      localization: { locale: LOCALE },
      layout: {
        background: { color: colors.bg },
        textColor: colors.fg,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: colors.line },
        horzLines: { color: colors.line },
      },
      rightPriceScale: { borderColor: colors.line },
      timeScale: { borderColor: colors.line, timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: colors.accent }, horzLine: { color: colors.accent } },
    });
    chart.current = instance;
    priceSeries.current = instance.addSeries(CandlestickSeries, {
      upColor: colors.up,
      downColor: colors.down,
      borderVisible: false,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    });
    // An overlay on the price pane (`priceScaleId: ""`), scaled to the
    // full height: the band marks WHEN, and has nothing to say about
    // price, so it must not squeeze the candles.
    bandSeries.current = instance.addSeries(HistogramSeries, {
      priceScaleId: "",
      color: "rgba(242, 181, 68, 0.22)",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    bandSeries.current.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
    volumeSeries.current = instance.addSeries(
      HistogramSeries,
      { priceFormat: { type: "volume" }, priceLineVisible: false },
      1,
    );
    markerApi.current = createSeriesMarkers(priceSeries.current, []);
    return () => {
      instance.remove();
      chart.current = null;
      priceSeries.current = null;
      volumeSeries.current = null;
      bandSeries.current = null;
      markerApi.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.applyOptions({ timeScale: { timeVisible } });
  }, [timeVisible]);

  useEffect(() => {
    const series = priceSeries.current;
    if (series === null) return;
    // Whitespace merged into the price series, sorted with it: the time
    // scale only has slots for instants a series mentions, and the band
    // is drawn at instants that by definition have no bar.
    const merged = [...stamp(candles), ...stamp(whitespace)].sort((a, b) => a.time - b.time);
    series.setData(merged);
  }, [candles, whitespace]);

  useEffect(() => {
    volumeSeries.current?.setData(stamp(volume));
  }, [volume]);

  // After the data effects above, so the scale is fitted to a series the
  // chart already has. Guarded on having any: fitting an empty series is
  // a no-op, and the first render of every panel is empty.
  const fitted = useRef<string | null>(null);
  useEffect(() => {
    if (candles.length === 0 || fitted.current === fitKey) return;
    fitted.current = fitKey ?? null;
    chart.current?.timeScale().fitContent();
  }, [candles, fitKey]);

  useEffect(() => {
    // Value 1 on a scale pinned to [0, 1] is a full-height column.
    bandSeries.current?.setData(stamp(band).map((slot) => ({ time: slot.time, value: 1 })));
  }, [band]);

  useEffect(() => {
    markerApi.current?.setMarkers(
      markers.map((marker) => ({
        time: marker.time as UTCTimestamp,
        position: "belowBar" as const,
        color: theme.accent,
        shape: MARKER_SHAPE[marker.kind],
        text: marker.text,
      })),
    );
  }, [markers, theme]);

  // The reader can change the accent while a chart is on screen. What is
  // painted on a canvas does not follow a stylesheet, so it is re-applied
  // here rather than waiting for the next symbol.
  useEffect(() => {
    chart.current?.applyOptions({
      layout: { background: { color: theme.bg }, textColor: theme.fg },
      grid: { vertLines: { color: theme.line }, horzLines: { color: theme.line } },
      rightPriceScale: { borderColor: theme.line },
      timeScale: { borderColor: theme.line },
      crosshair: { vertLine: { color: theme.accent }, horzLine: { color: theme.accent } },
    });
  }, [theme]);

  return <div className="chart" ref={host} role="img" aria-label={label} />;
}


// --- the comparison chart ---------------------------------------------------
// Several instruments, each reduced to one number per session. Not an
// SVG primitive: a multi-year line with a shared time axis, crosshair
// and pan is what this library draws, and one file touching it keeps
// that from becoming two chart stacks.

export interface LineSeriesSpec {
  key: string;
  label: string;
  /** Identity, from the group palette: this series is THIS symbol, and
   *  says nothing about whether it rose. */
  colour: string;
  points: LinePoint[];
}

export interface LineChartProps {
  series: LineSeriesSpec[];
  label: string;
  /** A dashed line to measure against -- 100 for a series normalised to
   *  its own start. Omitted draws none. */
  baseline?: number;
}

export function LineChart({ series, label, baseline }: LineChartProps): ReactElement {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  // Keyed by symbol, because the set of series changes: retyping the
  // command with one symbol fewer must remove one line, not redraw
  // every line under shifted colours.
  const lines = useRef(new Map<string, ISeriesApi<"Line">>());
  const baseLine = useRef<IPriceLine | null>(null);

  useEffect(() => {
    const element = host.current;
    if (element === null) return;
    const colors = vizTheme();
    const instance = createChart(element, {
      autoSize: true,
      localization: { locale: LOCALE },
      layout: {
        background: { color: colors.bg },
        textColor: colors.fg,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: colors.line },
        horzLines: { color: colors.line },
      },
      rightPriceScale: { borderColor: colors.line },
      timeScale: { borderColor: colors.line, timeVisible: false, secondsVisible: false },
      crosshair: { vertLine: { color: colors.accent }, horzLine: { color: colors.accent } },
    });
    chart.current = instance;
    const drawn = lines.current;
    return () => {
      instance.remove();
      chart.current = null;
      baseLine.current = null;
      drawn.clear();
    };
  }, []);

  useEffect(() => {
    const instance = chart.current;
    if (instance === null) return;
    const drawn = lines.current;
    const wanted = new Set(series.map((one) => one.key));
    for (const [key, line] of drawn) {
      if (!wanted.has(key)) {
        instance.removeSeries(line);
        drawn.delete(key);
      }
    }
    for (const one of series) {
      let line = drawn.get(one.key);
      if (line === undefined) {
        line = instance.addSeries(LineSeries, { lineWidth: 2, priceLineVisible: false });
        drawn.set(one.key, line);
      }
      // Applied every pass, not only on creation: the colours are
      // assigned by position, so dropping the first symbol recolours
      // every series after it.
      line.applyOptions({ color: one.colour, title: one.label });
      line.setData(stamp(one.points));
    }
  }, [series]);

  useEffect(() => {
    const first = series[0];
    const line = first === undefined ? undefined : lines.current.get(first.key);
    if (line !== undefined && baseLine.current !== null) {
      line.removePriceLine(baseLine.current);
      baseLine.current = null;
    }
    if (line === undefined || baseline === undefined) return;
    baseLine.current = line.createPriceLine({
      price: baseline,
      color: vizTheme().muted,
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: "start",
    });
  }, [series, baseline]);

  return <div className="chart" ref={host} role="img" aria-label={label} />;
}
