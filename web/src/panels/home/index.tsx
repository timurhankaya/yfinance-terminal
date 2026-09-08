// The home page: what the archive holds, right now, in one screen.
//
// Six blocks, each with its own read, its own loading and its own
// failure -- one slow calendar must not blank the markets. Everything
// below the fold waits until it is scrolled to, because the page opens
// in a dock panel as often as in a full window.
import { Layout, type PanelProps, type PanelSpec } from "../../commands/types";
import { usePanelRun } from "../../workspace/frame";
import { Guide } from "./Guide";
import { Markets } from "./Markets";
import { Movers } from "./Movers";
import { News } from "./News";
import { Sectors } from "./Sectors";
import { Week } from "./Week";

export function HOME({ symbol }: PanelProps) {
  const go = usePanelRun();
  // A block hands back what the reader clicked; the home page decides
  // which panel that is, so a block never needs to know the code.
  const openSymbol = (code: string) => (picked: string) =>
    go({ symbol: picked === "" ? symbol : picked, code: picked === "" ? code : "DES", args: {} });

  return (
    <section className="home">
      <p className="detail-meta">
        Type a symbol to open its detail — <code className="usage">AAPL</code>, then{" "}
        <code className="usage">GP</code> for the chart or <code className="usage">FA</code> for the
        statements. Market-wide pages are on the left of the function bar and need no symbol.
        Everything is UTC.
      </p>
      <div className="home-grid">
        <Markets onOpen={openSymbol("MKT")} />
        <Sectors onOpen={(key) => go({ symbol, code: "DOM", args: key === "" ? {} : { domain: key } })} />
        <News onOpen={() => go({ symbol, code: "DS", args: { name: "news" } })} />
        <Movers onOpen={openSymbol("EQS")} />
        <Week onOpen={() => go({ symbol, code: "CAL", args: {} })} />
        <Guide />
      </div>
    </section>
  );
}

export const HOME_PANEL: PanelSpec = {
  code: "HOME",
  title: "The archive right now: markets, sectors, movers, news and what is coming",
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: HOME,
};
