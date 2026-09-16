// DES: the symbol's identity and EVERY field of its latest `info`
// snapshot. A key the sections do not name lands in "Other" rather than
// being dropped; only null fields are left out, and they are counted.
// `des/info.ts` says what a key means, `des/layout.ts` where it goes;
// this file only wires the components to the address.
import { usePanelRun } from "../workspace/frame";
import type { ReactElement } from "react";
import { getSymbol, type SymbolDetail } from "../api/client";
import { Layout, type PanelArgs, type PanelProps, type PanelSpec } from "../commands/types";
import { EmptyCard, ErrorCard, LoadState, MissingCard, usePanelData } from "./common";
import { useArgs } from "./controls";
import { Fields } from "./des/Fields";
import { group } from "./des/info";
import { FieldTab, TrendRange, firstFilled, isFieldTab, isTrendRange, tabsOf } from "./des/layout";
import { Overview } from "./des/Overview";

//: Re-exported: the rest of the terminal and the tests import these from here.
export { SECTIONS, formatInfo, group, label } from "./des/info";

//: The default window: a month is the shortest one whose shape is a
//: trend rather than a session's noise, and it is one daily read.
export const DEFAULT_RANGE = TrendRange.M1;

export const DES_ARGS = `DES [${Object.values(FieldTab).join("|")}] [${Object.values(TrendRange).join("|")}]`;
const DES_USAGE = `Usage: ${DES_ARGS}`;

/** A tab and a trend window, in either order (the two name sets are
 *  disjoint) and both optional. A bare `DES` names neither: the panel
 *  opens on the first tab the snapshot filled. */
function parseArgs(tokens: string[]): PanelArgs {
  const args: PanelArgs = {};
  for (const token of tokens) {
    const value = token.toLowerCase();
    if (isFieldTab(value) && args.tab === undefined) args.tab = value;
    else if (isTrendRange(value) && args.range === undefined) args.range = value;
    else throw new Error(DES_USAGE);
  }
  return args;
}

//: A bookmarked or hand-edited address never went through `parseArgs`.
//: An unknown value is dropped rather than refused: the panel has a
//: perfectly good default for each and a broken link should still open.
function normalizeArgs(args: PanelArgs): PanelArgs {
  const { tab, range, ...rest } = args;
  const kept: PanelArgs = { ...rest };
  if (tab !== undefined && isFieldTab(tab)) kept.tab = tab;
  if (range !== undefined && isTrendRange(range)) kept.range = range;
  return kept;
}

export function DES({ symbol, args }: PanelProps): ReactElement | null {
  // symbol can be null in the general PanelProps shape (a panel row can be
  // rendered before a symbol is chosen); guard the load itself rather than
  // skipping the hook call, which React's rules of hooks forbid.
  const { state, retry } = usePanelData<SymbolDetail>(symbol ?? "", () =>
    symbol === null ? Promise.reject(new Error("no symbol")) : getSymbol(symbol),
  );
  const set = useArgs(DES_PANEL_CODE, symbol, args);
  const run = usePanelRun();

  if (symbol === null) return null;
  if (state.kind === LoadState.Loading) return <p className="muted">Loading {symbol}…</p>;
  if (state.kind === LoadState.Missing) return <MissingCard symbol={symbol} />;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  // An `EmptyCard`, not a bare `null`: a blank panel is indistinguishable
  // from a crash, and every other panel says what is missing.
  if (state.kind === LoadState.Empty) return <EmptyCard what="description" />;

  const detail = state.data;
  const grouped = group(detail.info ?? {});
  const tabs = tabsOf(grouped);
  const active = args.tab !== undefined && isFieldTab(args.tab) ? args.tab : firstFilled(tabs);
  const range = args.range !== undefined && isTrendRange(args.range) ? args.range : DEFAULT_RANGE;

  return (
    <section className="des-panel">
      <Overview detail={detail} onOpen={(code) => run({ symbol, code, args: {} })} range={range} onRange={(next) => set({ range: next })} />
      <Fields
        symbol={symbol}
        tabs={tabs}
        active={active}
        onPick={(tab) => set({ tab })}
        nulls={grouped.nulls}
        synced={detail.info !== null}
      />
    </section>
  );
}

const DES_PANEL_CODE = "DES";

export const DES_PANEL: PanelSpec = {
  code: DES_PANEL_CODE,
  title: "Description: identity and the whole info snapshot",
  needsSymbol: true,
  usage: DES_ARGS,
  layout: Layout.Headed,
  parseArgs,
  normalizeArgs,
  component: DES,
};
