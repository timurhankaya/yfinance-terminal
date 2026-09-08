// PG: the pages this browser has saved.
//
// A layout does not fit in an address, so a saved page is the one thing
// in the terminal a reader cannot find by typing its URL from memory.
// This is where they are: what is saved, which key opens it, and the two
// edits a list of pages needs -- rename and delete.
//
// It reads the store on every render rather than holding a copy: the
// store is written by the page the reader is actually on, and a second
// copy here would go stale the moment they arranged a panel.
import { useState } from "react";
import type { ReactElement } from "react";
import { useNavigate } from "react-router";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { useListKeys } from "../panels/common";
import { usePanelRun } from "./frame";
import { PageName } from "./page";
import { PAGE_KEYS, dropPage, readStore, renamePage, storageBlocked } from "./store";

export const PG_ARGS = "PG [SAVE <name>|<name>]";

/** A page's name as it is stored.
 *
 *  Lower-cased and spaces closed up, because the name IS the address
 *  (`/ui/w/trading`) and two pages a URL cannot tell apart are one page
 *  with a bug. `-` is reserved for the working page. */
export function normalizeName(raw: string): string | null {
  const name = raw.trim().toLowerCase().replace(/\s+/g, "-");
  if (name === "" || name === PageName.Scratch) return null;
  return /^[a-z0-9][a-z0-9._-]*$/.test(name) ? name : null;
}

/** The path a saved page lives at. */
export function pagePath(name: string): string {
  return `/ui/w/${encodeURIComponent(name)}`;
}

//: The typed forms (`PG SAVE x`, `PG x`) are the `PG` ACTION's, and the
//: parser resolves actions first. The panel is reachable from the
//: function bar and from a hand-typed `/ui/m/PG`, neither of which
//: carries arguments.
function parseArgs(): PanelArgs {
  return {};
}

export function PG({ symbol }: PanelProps): ReactElement {
  const run = usePanelRun();
  const navigate = useNavigate();
  // A counter, not the store: the store is the truth and is re-read
  // after every edit, but React has to be told an edit happened.
  const [edits, setEdits] = useState(0);
  const store = readStore();
  const names = store.order;
  const [selected, setSelected] = useListKeys(names.length, (index) => {
    const name = names[index];
    if (name !== undefined) open(name);
  });

  function open(name: string): void {
    // A page is not a panel. Opening one changes what the whole window
    // is, so it goes through the address rather than through this
    // panel's frame -- which would have replaced this list with the page
    // inside itself.
    void navigate(pagePath(name));
  }

  function rename(from: string): void {
    const typed = window.prompt(`Rename ${from} to`, from);
    if (typed === null) return;
    const to = normalizeName(typed);
    if (to === null) return;
    renamePage(from, to);
    setEdits(edits + 1);
  }

  return (
    <section className="pages">
      <p className="detail-meta">
        {names.length === 0
          ? "No saved pages yet."
          : `${names.length} saved ${names.length === 1 ? "page" : "pages"}. Enter or click opens one.`}
      </p>
      {storageBlocked() && (
        <p className="card card-error">
          This browser will not let the terminal store anything, so pages last only as long as
          the tab. A private window does this.
        </p>
      )}
      {names.length === 0 ? (
        <p className="muted">
          Arrange a page, then type <code className="usage">PG SAVE trading</code> to name it. The
          name is the address: <code className="usage">/ui/w/trading</code>.
        </p>
      ) : (
        <ul className="list" role="listbox" aria-label="saved pages">
          {names.map((name, index) => (
            <li
              key={name}
              role="option"
              tabIndex={-1}
              className={index === selected ? "list-row row-selected" : "list-row"}
              aria-selected={index === selected}
              onClick={() => {
                setSelected(index);
                open(name);
              }}
            >
              <span className="strip-symbol">{PAGE_KEYS[index] ?? "—"}</span>{" "}
              <span className="ds-name">{name}</span>{" "}
              <span className="muted">
                {panelCount(store.pages[name]?.dock)} panels ·{" "}
                {letters(store.pages[name]?.groups)}
              </span>{" "}
              <button
                type="button"
                className="help-fn"
                onClick={(event) => {
                  event.stopPropagation();
                  rename(name);
                }}
              >
                rename
              </button>{" "}
              <button
                type="button"
                className="help-fn"
                onClick={(event) => {
                  event.stopPropagation();
                  dropPage(name);
                  setEdits(edits + 1);
                }}
              >
                delete
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="chart-legend">
        <span className="muted">
          {PAGE_KEYS.join(", ")} open the pages in this order. F5, F11 and F12 belong to the
          browser and cannot be taken; F6 is the address bar.
        </span>
        <button type="button" className="help-fn" onClick={() => run({ symbol, code: "HOME", args: {} })}>
          Home
        </button>
      </p>
    </section>
  );
}

function panelCount(dock: unknown): number {
  if (typeof dock !== "object" || dock === null) return 0;
  const panels = (dock as { panels?: unknown }).panels;
  return typeof panels === "object" && panels !== null ? Object.keys(panels).length : 0;
}

function letters(groups: Record<string, string> | undefined): string {
  const entries = Object.entries(groups ?? {});
  if (entries.length === 0) return "no letters";
  return entries.map(([letter, symbol]) => `${letter.toUpperCase()}=${symbol}`).join(" ");
}

export const PG_PANEL: PanelSpec = {
  code: "PG",
  title: "Saved pages",
  usage: PG_ARGS,
  needsSymbol: false,
  layout: Layout.Single,
  parseArgs,
  component: PG,
};
