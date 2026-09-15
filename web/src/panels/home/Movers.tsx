// What moved, from the screens this deployment runs.
//
// The screen keys are a deployment's choice, not ours: a terminal that
// hard-coded `day_gainers` would show an error card wherever that screen
// is off. The block asks which screens exist, takes the two that name
// themselves gainers and losers, and says what it found when it finds
// neither.
import { getScreen, getScreens } from "../../api/client";
import type { ScreenRow, ScreenSummary } from "../../api/client";
import { LoadState, usePanelData } from "../common";
import { asNumber, formatPrice } from "../format";
import { SparkCell, useSparklines } from "../spark";
import { Block, Failed, Waiting } from "./Block";

const ROWS = 8;

interface Movers {
  gainers: { screen: ScreenSummary; rows: ScreenRow[] } | null;
  losers: { screen: ScreenSummary; rows: ScreenRow[] } | null;
  available: string[];
}

/** The screen whose key says it is this side of the market. */
export function pickScreen(screens: ScreenSummary[], want: "gain" | "los"): ScreenSummary | null {
  return screens.find((screen) => screen.screen_key.toLowerCase().includes(want)) ?? null;
}

export function Movers({ onOpen }: { onOpen: (symbol: string) => void }) {
  return (
    <Block title="Movers" onOpen={() => onOpen("")} wide>
      {(visible) => (visible ? <MoversBody onOpen={onOpen} /> : <Waiting what="movers" />)}
    </Block>
  );
}

function MoversBody({ onOpen }: { onOpen: (symbol: string) => void }) {
  const { state } = usePanelData<Movers>("home-movers", async () => {
    const screens = await getScreens();
    const gain = pickScreen(screens, "gain");
    const lose = pickScreen(screens, "los");
    const [gainers, losers] = await Promise.all([
      gain === null ? null : getScreen(gain.screen_key, 0),
      lose === null ? null : getScreen(lose.screen_key, 0),
    ]);
    return {
      gainers: gainers === null || gain === null ? null : { screen: gain, rows: gainers.rows.slice(0, ROWS) },
      losers: losers === null || lose === null ? null : { screen: lose, rows: losers.rows.slice(0, ROWS) },
      available: screens.map((screen) => screen.screen_key),
    };
  });

  const data = state.kind === LoadState.Ready ? state.data : null;
  const symbols = [
    ...(data?.gainers?.rows ?? []).map((row) => row.symbol),
    ...(data?.losers?.rows ?? []).map((row) => row.symbol),
  ];
  const spark = useSparklines(symbols);

  if (state.kind === LoadState.Loading) return <Waiting what="movers" />;
  if (state.kind === LoadState.Error) return <Failed what="Movers" message={state.message} />;
  if (data === null) return null;
  if (data.gainers === null && data.losers === null) {
    return (
      <p className="muted">
        No gainers or losers screen is enabled here.{" "}
        {data.available.length === 0
          ? "No screens are enabled at all."
          : `Enabled: ${data.available.join(", ")}.`}
      </p>
    );
  }

  return (
    <div className="home-movers">
      {[data.gainers, data.losers].map((side, index) =>
        side === null ? null : (
          <div key={side.screen.screen_key}>
            <p className="detail-meta">{side.screen.title}</p>
            <ul className="list">
              {side.rows.map((row) => (
                <li
                  key={row.symbol}
                  className="list-row"
                >
                  <button type="button" className="mover-row" onClick={() => onOpen(row.symbol)} aria-label={`Open ${row.symbol}`}>
                    <span className="ds-name">{row.symbol}</span>{" "}
                    <span className="num">{formatPrice(row.price)}</span>{" "}
                    <span className="num"><Move percent={asNumber(row.change_percent)} /></span>{" "}
                    <span className="mover-trend"><SparkCell data={spark} symbol={row.symbol} /></span>
                  </button>
                </li>
              ))}
            </ul>
            {index === 0 && side.rows.length === 0 && (
              <p className="muted">The roster is empty; the screen has not run yet.</p>
            )}
          </div>
        ),
      )}
    </div>
  );
}

function Move({ percent }: { percent: number | null }) {
  if (percent === null) return <span className="muted">—</span>;
  return (
    <span className={percent > 0 ? "up" : percent < 0 ? "down" : undefined}>
      {percent > 0 ? "+" : ""}
      {percent.toFixed(2)}%
    </span>
  );
}
