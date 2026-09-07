import { useNavigate } from "react-router";
import { commandToPath } from "../commands/parser";
import { listPanels } from "../commands/registry";
import type { PanelProps, PanelSpec } from "../commands/types";

export function HELP({ symbol }: PanelProps) {
  const navigate = useNavigate();
  const panels = listPanels();
  return (
    <section>
      <h2>Help</h2>
      <p>
        Type a command in the bar: a bare symbol switches the strip, and <code>SYMBOL CODE args…</code> or{" "}
        <code>CODE args…</code> runs a panel against the current or given symbol. Click a function below to
        run it{symbol ? ` on ${symbol}` : " (type a symbol first for the ones that need one)"}.
      </p>
      <ul className="list">
        {panels.map((panel) => {
          const runnable = !panel.needsSymbol || symbol !== null;
          return (
            <li key={panel.code} className="list-row">
              <button
                type="button"
                className="help-fn"
                disabled={!runnable}
                onClick={() => void navigate(commandToPath({ symbol, code: panel.code, args: {} }))}
              >
                {panel.code}
              </button>{" "}
              — {panel.title} ({panel.needsSymbol ? "needs a symbol" : "no symbol"})
              {panel.usage && (
                <>
                  {" "}
                  <code className="usage">{panel.usage}</code>
                </>
              )}
            </li>
          );
        })}
      </ul>
      <p>
        Shortcuts: <code>Ctrl/⌘+K</code> or <code>/</code> focus the command bar, <code>Esc</code> clears it
        (and, when it is not focused, goes back), <code>Shift+Esc</code> goes forward, <code>?</code> opens
        this help.
      </p>
      <p>
        Inside a list panel, with the command bar unfocused: <code>j</code>/<code>k</code> move the selection
        and <code>Enter</code> opens the selected row.
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
