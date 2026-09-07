// HELP: the terminal's guide. One page that a first-time reader can
// follow top to bottom: what to type, every function with its usage,
// every shortcut, and how tables and links behave. The function list is
// read from the registry so it can never drift from what is installed.
import { useNavigate } from "react-router";
import { commandToPath } from "../commands/parser";
import { listPanels } from "../commands/registry";
import { Layout, type PanelProps, type PanelSpec } from "../commands/types";

//: Functions in reading order, grouped. A registered code not named
//: here still shows, under "Other".
const GROUPS: ReadonlyArray<[title: string, codes: string[]]> = [
  ["About one symbol", ["DES", "FA", "ANR", "N", "CF", "CA", "PX", "HDS", "ERN", "FUND", "REF"]],
  ["Market-wide", ["CAL", "MKT", "SCR", "SRCH", "DOM"]],
  ["Everything in the archive", ["DS"]],
  ["This page", ["HELP"]],
];

const EXAMPLES: ReadonlyArray<[command: string, what: string]> = [
  ["AAPL", "Open Apple: the description (DES) with every field of its snapshot."],
  ["FA", "Financial statements for the symbol on the strip (income, annual by default)."],
  ["FA balance quarterly", "Same function, with arguments: balance sheet, quarterly."],
  ["MSFT N", "Symbol and function in one line: Microsoft's news."],
  ["HDS trades", "Holders panel, opened on the insider-transactions tab."],
  ["PX 5m 100", "The newest 100 five-minute bars as a table."],
  ["CAL economic region=US", "A market-wide panel with a tab and a filter."],
  ["DS", "The catalogue: every dataset the archive holds, by family."],
  ["DS earnings_calendar", "Any dataset by name, filtered to the strip's symbol when it has one."],
  ["CF DES", "CF Industries' description: two function codes in a row mean the first is a symbol."],
  ["BRK-B", "Symbols as Yahoo writes them: BRK-B, ^GSPC, EURUSD=X, BTC-USD."],
];

const SHORTCUTS: ReadonlyArray<[keys: string, where: string, what: string]> = [
  ["Ctrl+K or ⌘+K", "anywhere", "Focus the command box."],
  ["/", "outside a text field", "Focus the command box."],
  ["Enter", "in the command box", "Run the command. The box then hands the keyboard to the panel."],
  ["Esc", "in the command box", "Clear it and leave it, so the panel's keys work."],
  ["Esc", "outside a text field", "Go back one step (browser history: every command is a step)."],
  ["Shift+Esc", "outside a text field", "Go forward one step."],
  ["?", "outside a text field", "Open this page, keeping the current symbol."],
  ["j / k", "in a list or table", "Move the selection down / up."],
  ["Enter", "in a list or table", "Open the selected row: the article, the filing's exhibits, or every field of the row."],
  ["Click", "on a row, tab or function", "Same as the keys: rows open, tabs switch, functions run on the strip's symbol."],
];

function FunctionRow({ panel, symbol }: { panel: PanelSpec; symbol: string | null }) {
  const navigate = useNavigate();
  const runnable = !panel.needsSymbol || symbol !== null;
  return (
    <li className="list-row">
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
}

export function HELP({ symbol }: PanelProps) {
  const panels = listPanels();
  const named = new Set(GROUPS.flatMap(([, codes]) => codes));
  const groups: Array<[string, PanelSpec[]]> = [];
  for (const [title, codes] of GROUPS) {
    const list = codes.flatMap((code) => panels.filter((p) => p.code === code));
    if (list.length > 0) groups.push([title, list]);
  }
  const other = panels.filter((p) => !named.has(p.code));
  if (other.length > 0) groups.push(["Other", other]);

  return (
    <section className="guide">
      <h2>How to use the terminal</h2>
      <p>
        One box, one line: <code>SYMBOL FUNCTION ARGUMENTS</code>. Any part can be left out. A bare symbol
        keeps the current function; a bare function keeps the current symbol (shown on the strip under the
        function bar). Press Enter to run. Everything the terminal shows is the archive's copy of Yahoo
        Finance data, read through this deployment's own API.
        {symbol ? (
          <>
            {" "}
            The symbol on the strip is <strong>{symbol}</strong>; the functions below run on it.
          </>
        ) : (
          " Type a symbol first for the functions that need one."
        )}
      </p>

      <h3>Examples</h3>
      <table className="grid">
        <thead>
          <tr>
            <th>Type</th>
            <th>What happens</th>
          </tr>
        </thead>
        <tbody>
          {EXAMPLES.map(([command, what]) => (
            <tr key={command}>
              <td>
                <code>{command}</code>
              </td>
              <td>{what}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Functions</h3>
      <p>
        Click a code to run it. Arguments are optional and positional; <code>[a|b|c]</code> lists the
        choices, <code>filter=value</code> narrows a dataset by one of the filters the catalogue lists for it.
      </p>
      {groups.map(([title, list]) => (
        <div key={title}>
          <h4>{title}</h4>
          <ul className="list">
            {list.map((panel) => (
              <FunctionRow key={panel.code} panel={panel} symbol={symbol} />
            ))}
          </ul>
        </div>
      ))}

      <h3>Keyboard</h3>
      <table className="grid">
        <thead>
          <tr>
            <th>Keys</th>
            <th>Where</th>
            <th>What</th>
          </tr>
        </thead>
        <tbody>
          {SHORTCUTS.map(([keys, where, what]) => (
            <tr key={`${keys}|${where}`}>
              <td>
                <code>{keys}</code>
              </td>
              <td className="muted">{where}</td>
              <td>{what}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Tables, details and links</h3>
      <ul className="list">
        <li className="list-row">
          Every table shows every column the dataset has, except <code>raw_json</code>. Enter or a click on a
          row opens the row detail with every field, the raw JSON included, at full precision.
        </li>
        <li className="list-row">
          The <code>open</code> column, where present, links to the page a row implies: the SEC filing on
          EDGAR, the analyst report or the sector page on Yahoo Finance, a symbol's quote page. URL values in
          any column are links too. Links open in a new tab and send no referrer.
        </li>
        <li className="list-row">
          Numbers use a fixed en-US format (1,234.56; 4.67T for large values); date-times are UTC. Decimals
          below one keep four places in the grid and the exact value in the detail.
        </li>
        <li className="list-row">
          A panel fetches up to five pages of 1,000 rows and says so when the list was cut; add a filter to
          narrow it. If a symbol is unknown, a search palette opens with matching tickers.
        </li>
        <li className="list-row">
          The URL is the whole state: <code>/ui/t/AAPL/FA?statement=balance_sheet&amp;freq=quarterly</code>{" "}
          can be bookmarked or shared, and the browser's back button retraces your commands.
        </li>
      </ul>
    </section>
  );
}

export const HELP_PANEL: PanelSpec = {
  code: "HELP",
  title: "Help: how to use the terminal, every function and shortcut",
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs: () => ({}),
  component: HELP,
};
