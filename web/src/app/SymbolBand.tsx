// The band a symbol page wears: what the symbol is doing, and every
// other function that can be pointed at it.
//
// It lives inside the panel rather than above the dock, for exactly the
// reason the strip does (`Workspace.tsx`): two panels in two groups are
// two symbols, and one band over the page could only have told the truth
// about one of them.
//
// Two questions decide what it shows, and they used to be one:
//   * `needsSymbol` -- does this panel have a symbol to navigate from?
//     Fourteen do, and every one of them gets the band. Only four were
//     `Headed`, so ten of them had no way back except the command line.
//   * `Layout.Headed` -- is there a live price to put in it? That is what
//     the layout field has always meant, and `Strip` already carries the
//     `live={false}` case.
//
// The codes are the function bar's, minus the market pages, which stay
// under the command line where they belong: a page about the market is
// not a page about AAPL, and one flat list of twenty-two codes never
// said which was which.
import { useEffect, useRef } from "react";
import type { ReactElement } from "react";
import { listPanels } from "../commands/registry";
import { usePanelRun } from "../workspace/frame";
import { Strip } from "./Strip";

export function SymbolBand(props: {
  symbol: string;
  /** The code showing in this panel, which is the tab that reads as on. */
  code: string;
  live: boolean;
}): ReactElement {
  const { symbol, code, live } = props;
  const run = usePanelRun();
  const strip = useRef<HTMLElement>(null);
  const active = useRef<HTMLButtonElement>(null);

  // A narrow panel scrolls its tabs rather than wrapping them to three
  // rows, so the one that is on has to be brought into view -- otherwise
  // opening `REF` in a split leaves the reader looking at `ANR`.
  //
  // By `scrollLeft` on the strip itself, NOT by `scrollIntoView`: that
  // scrolls every scrollable ancestor, and the band is sticky inside the
  // panel's own scrollport -- so it pushed the overview up underneath
  // itself on every mount. This can only ever move the strip sideways.
  useEffect(() => {
    const nav = strip.current;
    const button = active.current;
    if (nav === null || button === null) return;
    const right = button.offsetLeft + button.offsetWidth;
    if (button.offsetLeft < nav.scrollLeft) nav.scrollLeft = button.offsetLeft;
    else if (right > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = right - nav.clientWidth;
  }, [code]);

  return (
    <div className="band">
      <Strip symbol={symbol} live={live} />
      <nav className="band-tabs" ref={strip} aria-label={`${symbol} functions`}>
        {listPanels()
          .filter((panel) => panel.needsSymbol)
          .map((panel) => {
            const on = panel.code === code;
            return (
              <button
                key={panel.code}
                type="button"
                ref={on ? active : undefined}
                className={on ? "band-tab band-tab-on" : "band-tab"}
                aria-current={on ? "page" : undefined}
                title={panel.title}
                onClick={() => run({ symbol, code: panel.code, args: {} })}
              >
                {panel.code}
              </button>
            );
          })}
      </nav>
    </div>
  );
}
