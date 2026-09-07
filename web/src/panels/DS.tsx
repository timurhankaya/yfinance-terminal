// DS: the catalogue, and any dataset in it. Without a name it lists every
// dataset the API serves, grouped by family; with one it shows the rows.
// This is the panel that guarantees nothing in the archive is unreachable
// from the terminal, whether or not a curated panel covers it.
import { useMemo } from "react";
import { useNavigate } from "react-router";
import { getCatalog, type CatalogEntry } from "../api/client";
import { commandToPath } from "../commands/parser";
import type { PanelArgs, PanelProps, PanelSpec } from "../commands/types";
import { ErrorCard, useListKeys, usePanelData } from "./common";
import { DatasetView, filtersOf, parseFilters } from "./dataset";

export const DS_USAGE = "Usage: DS [dataset] [filter=value ...]  (DS alone lists every dataset)";

function parseArgs(tokens: string[]): PanelArgs {
  const [first, ...rest] = tokens;
  if (first === undefined) return {};
  if (first.includes("=")) throw new Error(DS_USAGE);
  return { name: first.toLowerCase(), ...parseFilters(rest, DS_USAGE) };
}

function Catalog({ symbol }: { symbol: string | null }) {
  const navigate = useNavigate();
  const { state, retry } = usePanelData<CatalogEntry[]>("catalog", getCatalog);
  const entries = useMemo(() => {
    if (state.kind !== "ready") return [];
    return [...state.data].sort((a, b) => a.family.localeCompare(b.family) || a.name.localeCompare(b.name));
  }, [state]);
  const open = (index: number) => {
    const entry = entries[index];
    if (entry) void navigate(commandToPath({ symbol, code: "DS", args: { name: entry.name } }));
  };
  const [selected, setSelected] = useListKeys(entries.length, open);

  if (state.kind === "loading") return <p className="muted">Loading the catalogue…</p>;
  if (state.kind === "error") return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind !== "ready") return <p className="muted">The catalogue is empty.</p>;

  let family = "";
  return (
    <section>
      <p className="detail-meta">
        {entries.length} datasets. Enter or click opens one; add <code>filter=value</code> tokens to narrow it.
      </p>
      <ul className="list" role="listbox" aria-label="datasets">
        {entries.map((entry, index) => {
          const heading = entry.family !== family;
          family = entry.family;
          return (
            <li
              key={entry.name}
              role="option"
              tabIndex={-1}
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
  return <DatasetView name={name} symbol={symbol} filters={filtersOf(args, ["name"])} mode="auto" />;
}

export const DS_PANEL: PanelSpec = {
  code: "DS",
  title: "Datasets: the whole catalogue, any dataset by name",
  usage: "DS [dataset] [filter=value ...]",
  needsSymbol: false,
  layout: "single",
  parseArgs,
  component: DS,
};
