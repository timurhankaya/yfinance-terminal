// DS: the catalogue, and any dataset in it. Without a name it lists every
// dataset the API serves, grouped by family; with one it shows the rows.
// This is the panel that guarantees nothing in the archive is unreachable
// from the terminal, whether or not a curated panel covers it.
import { useMemo } from "react";
import { getCatalog, type CatalogEntry } from "../api/client";
import { usePanelRun } from "../workspace/frame";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { ErrorCard, LoadState, useListKeys, usePanelData } from "./common";
import { DatasetView, SymbolMode, filtersOf, parseFilters } from "./dataset";

export const DS_USAGE = "Usage: DS [dataset] [filter=value ...]  (DS alone lists every dataset)";

function parseArgs(tokens: string[]): PanelArgs {
  const [first, ...rest] = tokens;
  if (first === undefined) return {};
  if (first.includes("=")) throw new Error(DS_USAGE);
  return { name: first.toLowerCase(), ...parseFilters(rest, DS_USAGE) };
}

function Catalog({ symbol }: { symbol: string | null }) {
  const go = usePanelRun();
  const { state, retry } = usePanelData<CatalogEntry[]>("catalog", getCatalog, (entries) => entries.length === 0);
  const entries = useMemo(() => {
    if (state.kind !== LoadState.Ready) return [];
    return [...state.data].sort((a, b) => a.family.localeCompare(b.family) || a.name.localeCompare(b.name));
  }, [state]);
  const open = (index: number) => {
    const entry = entries[index];
    if (entry) go({ symbol, code: "DS", args: { name: entry.name } });
  };
  const [selected, setSelected] = useListKeys(entries.length, open);

  if (state.kind === LoadState.Loading) return <p className="muted">Loading the catalogue…</p>;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind === LoadState.Empty) return <p className="muted">The catalogue is empty.</p>;
  if (state.kind !== LoadState.Ready) return null;

  let family = "";
  return (
    <section>
      <p className="detail-meta">
        {entries.length} datasets. Enter or click opens one; add <code>filter=value</code> tokens to narrow it.
      </p>
      {/* Focusable, with the active option named: the j/k/Enter model
          lives on a window listener, so without these the whole keyboard
          interaction is unreachable by Tab and invisible to a reader. */}
      <ul
        className="list"
        role="listbox"
        aria-label="datasets"
        tabIndex={0}
        aria-activedescendant={entries.length > 0 ? `ds-entry-${selected}` : undefined}
      >
        {entries.map((entry, index) => {
          const heading = entry.family !== family;
          family = entry.family;
          return (
            <li
              key={entry.name}
              id={`ds-entry-${index}`}
              role="option"
              className={index === selected ? "list-row row-selected" : "list-row"}
              aria-selected={index === selected}
              data-family={heading ? entry.family : undefined}
              onClick={() => {
                setSelected(index);
                open(index);
              }}
            >
              {heading && <span className="family">{entry.family}</span>}
              <span className="ds-name">{entry.name}</span>{" "}
              <span className="muted">
                {entry.symbol_scoped ? "per symbol" : "market-wide"}
                {entry.filters.length > 0 ? ` · ${entry.filters.map((f) => `${f}=`).join(" ")}` : ""}
                {" · "}
                {entry.columns.length} columns
              </span>{" "}
              <span>{entry.description}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function DS({ symbol, args }: PanelProps) {
  const name = args.name;
  if (name === undefined) return <Catalog symbol={symbol} />;
  return <DatasetView name={name} symbol={symbol} filters={filtersOf(args, ["name"])} mode={SymbolMode.Auto} />;
}

export const DS_PANEL: PanelSpec = {
  code: "DS",
  title: "Datasets: the whole catalogue, any dataset by name",
  usage: "DS [dataset] [filter=value ...]",
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs,
  component: DS,
};
