import { listPanels } from "../commands/registry";
import type { PanelSpec } from "../commands/types";

export function HELP() {
  const panels = listPanels();
  return (
    <section>
      <h2>Help</h2>
      <p>
        Type a command in the bar: a bare symbol switches the strip, and <code>SYMBOL CODE args…</code> or{" "}
        <code>CODE args…</code> runs a panel against the current or given symbol.
      </p>
      <ul className="list">
        {panels.map((panel) => (
          <li key={panel.code} className="list-row">
            {panel.code} — {panel.title} ({panel.needsSymbol ? "needs a symbol" : "no symbol"})
          </li>
        ))}
      </ul>
      <p>
        Shortcuts: <code>Ctrl/⌘+K</code> focuses the command bar, <code>/</code> starts a search, <code>Esc</code>{" "}
        clears the command bar, <code>Shift+Esc</code> closes the panel, and <code>?</code> opens this help.
      </p>
      <p>
        Inside a list panel: <code>j</code>/<code>k</code> move the selection and <code>Enter</code> opens the
        selected row.
      </p>
    </section>
  );
}

export const HELP_PANEL: PanelSpec = {
  code: "HELP",
  title: "Help",
  needsSymbol: false,
  layout: "single",
  parseArgs: () => ({}),
  component: HELP,
};
