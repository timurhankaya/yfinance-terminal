// The one place `lightweight-charts` is touched.
//
// Everything decided rather than drawn is in `chart-data.ts`; this holds
// the imperative half -- a chart instance, its series, and the effects
// that keep them in step with props. Two panels use it (`GP` daily,
// `GIP` intraday) and neither knows the library exists.
//
// Two panes, not two price scales: volume on the same scale as price
// flattens a $200 candle against 40 million shares. The gap band is the
// exception and IS an overlay on the price pane, because it has no
// values of its own -- it is a full-height mark at instants the archive
// is missing.
//
// The library writes its own styles through the CSSOM (`el.style.x = v`),
// which the page's `style-src 'self'` allows; it injects no `<style>`
// element, which that directive would block.
import { useEffect, useRef } from "react";
import type { ReactElement } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";
import type {
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import { DOWN_COLOR, MarkerKind, UP_COLOR } from "./chart-data";
import type { ActionMarker, Candle, VolumeBar, Whitespace } from "./chart-data";

//: Read once from the stylesheet so the chart cannot drift from the rest
//: of the terminal when a colour changes. The fallbacks are the same
//: values `styles.css` sets, for a test environment with no computed
//: style to read.
function theme(): { bg: string; fg: string; grid: string; accent: string } {
  const style = getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;
  return {
    bg: read("--bg", "#0b0e11"),
    fg: read("--fg", "#d7dde3"),
    grid: read("--line", "#1f262e"),
    accent: read("--accent", "#f2b544"),
  };
}

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
  const { candles, volume, markers, whitespace, band, timeVisible, label } = props;
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
    const colors = theme();
    const instance = createChart(element, {
      // ResizeObserver, so the chart follows the panel without a listener
      // of ours; width/height are the fallback if it is unavailable.
      autoSize: true,
      layout: {
        background: { color: colors.bg },
        textColor: colors.fg,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid },
      },
      rightPriceScale: { borderColor: colors.grid },
      timeScale: { borderColor: colors.grid, timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: colors.accent }, horzLine: { color: colors.accent } },
    });
    chart.current = instance;
    priceSeries.current = instance.addSeries(CandlestickSeries, {
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderVisible: false,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
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

  useEffect(() => {
    // Value 1 on a scale pinned to [0, 1] is a full-height column.
    bandSeries.current?.setData(stamp(band).map((slot) => ({ time: slot.time, value: 1 })));
  }, [band]);

  useEffect(() => {
    markerApi.current?.setMarkers(
      markers.map((marker) => ({
        time: marker.time as UTCTimestamp,
        position: "belowBar" as const,
        color: theme().accent,
        shape: MARKER_SHAPE[marker.kind],
        text: marker.text,
      })),
    );
  }, [markers]);

  return <div className="chart" ref={host} role="img" aria-label={label} />;
}
