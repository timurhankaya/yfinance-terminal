// The band a symbol page wears: what the symbol is doing, and every
// other function that can be pointed at it. It lives inside the panel
// rather than above the dock: two panels in two groups are two symbols.
// `needsSymbol` decides whether a panel gets the band; `Layout.Headed`
// decides whether there is a live price to put in it.
import { useEffect, useRef } from "react";
import type { ReactElement } from "react";
import { listPanels } from "../commands/registry";
import { usePanelRun } from "../workspace/frame";
import { Strip } from "./Strip";
import { functionHelp } from "./tab-help";

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

  // A narrow panel scrolls its tabs, so the active one is brought into
  // view by `scrollLeft` on the strip itself, NOT `scrollIntoView`: that
  // scrolls every scrollable ancestor, and the band is sticky inside the
  // panel's own scrollport, so it would push the overview up on mount.
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
                data-tooltip={functionHelp(panel)}
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
