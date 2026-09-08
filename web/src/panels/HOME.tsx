// HOME: what `/ui` is.
//
// The terminal used to open on an empty `DES` -- a symbol page with no
// symbol, which shows nothing and explains nothing. A reader arriving
// for the first time had no way to learn what the archive holds without
// already knowing a mnemonic to type.
//
// So the landing page is the market-wide view: is anything open, which
// screens ran today, and a way in by symbol. Every card is a link to the
// panel that owns that data, so the home page is an index rather than a
// second implementation -- nothing is rendered here that is not one
// keystroke away anyway.
import { useMemo } from "react";
import type { ReactElement } from "react";
import { getDatasetPage, getScreens, type Row, type ScreenSummary } from "../api/client";
import { usePanelRun } from "../workspace/frame";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";
import { LoadState, usePanelData } from "./common";
import { countLabel, runLabel } from "./EQS";
import { text } from "./format";

//: Screens on the card. The rest are one Enter away in `EQS`, and a
//: landing page that lists nineteen of anything is a directory, not a
//: summary.
const SCREEN_PREVIEW = 6;

interface Overview {
  status: Row[];
  screens: ScreenSummary[];
}

function Card(props: { title: string; go: () => void; children: ReactElement }): ReactElement {
  return (
    <section className="home-card">
      <h3>
        <button type="button" className="help-fn" onClick={props.go}>
          {props.title}
        </button>
      </h3>
      {props.children}
    </section>
  );
}

export function HOME({ symbol }: PanelProps) {
  const go = usePanelRun();
  const { state, retry } = usePanelData<Overview>("home", async () => {
    // Both reads are market-wide and neither is large. They are asked
    // for together so the page paints once rather than twice.
    const [status, screens] = await Promise.all([
      getDatasetPage("market_status", {}, null, 12),
      getScreens(),
    ]);
    return { status: status.rows, screens };
  });

  const overview = state.kind === LoadState.Ready ? state.data : null;
  const screens = useMemo(
    () => (overview?.screens ?? []).slice(0, SCREEN_PREVIEW),
    [overview],
  );

  return (
    <section className="home">
      <p className="detail-meta">
        Type a symbol to open its detail — <code className="usage">AAPL</code>, then{" "}
        <code className="usage">GP</code> for the chart or <code className="usage">FA</code> for
        the statements. Market-wide pages are on the left of the function bar and need no symbol.
        Everything is UTC.
      </p>

      {state.kind === LoadState.Error && (
        <p className="card card-error">
          Could not reach the archive: {state.message}{" "}
          <button onClick={retry}>Retry</button>
        </p>
      )}

      <div className="home-cards">
        <Card
          title="Markets"
          go={() => go({ symbol, code: "MKT", args: {} })}
        >
          {overview === null ? (
            <p className="muted">Loading…</p>
          ) : overview.status.length === 0 ? (
            <p className="muted">No market status in the archive yet.</p>
          ) : (
            <ul className="list">
              {overview.status.map((row, index) => (
                <li key={`${text(row, "market_id")}-${String(index)}`} className="list-row">
                  <span className="ds-name">{text(row, "market_id")}</span>{" "}
                  <span className="muted">{text(row, "status")}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Screens" go={() => go({ symbol, code: "EQS", args: {} })}>
          {overview === null ? (
            <p className="muted">Loading…</p>
          ) : screens.length === 0 ? (
            <p className="muted">No screens are enabled.</p>
          ) : (
            <ul className="list">
              {screens.map((screen) => (
                <li
                  key={screen.screen_key}
                  className="list-row"
                  onClick={() => go({ symbol, code: "EQS", args: { screen: screen.screen_key } })}
                >
                  <span className="ds-name">{screen.screen_key}</span>{" "}
                  <span className="muted">
                    {countLabel(screen)} · {runLabel(screen)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Calendars" go={() => go({ symbol, code: "CAL", args: {} })}>
          <p className="muted">
            Earnings, economic releases, IPOs and splits — what the archive has ahead.
          </p>
        </Card>

        <Card title="Everything" go={() => go({ symbol, code: "DS", args: {} })}>
          <p className="muted">
            The catalogue: every dataset the archive holds, by family, with its filters and
            columns.
          </p>
        </Card>
      </div>
    </section>
  );
}

export const HOME_PANEL: PanelSpec = {
  code: "HOME",
  title: "The market, and the way in",
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: HOME,
};
