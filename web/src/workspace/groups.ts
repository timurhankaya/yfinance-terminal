// Which symbol a panel is about, when a page has more than one.
//
// A letter belongs to the panel, not to the box it sits in: dragging a
// panel across the page does not change what it is watching. That is the
// whole point of the letter -- `AAPL` typed once turns every panel in
// group A, wherever they are.
//
// The letters and their colours are `viz/colors`, not a second set here:
// the same seven identities the comparison chart draws with.
import { Group, GROUP_ORDER } from "../panels/viz/colors";
import type { PanelSpec } from "../commands/types";

export { Group, GROUP_ORDER };

/** The symbol each letter is currently pointed at. */
export type GroupSymbols = Partial<Record<Group, string>>;

/** `A`, for a letter the page shows. */
export function groupLabel(group: Group): string {
  return group.toUpperCase();
}

/** The letter a token names, or null. Accepts either case. */
export function parseGroup(token: string): Group | null {
  const lower = token.toLowerCase();
  return GROUP_ORDER.find((group) => group === lower) ?? null;
}

/** Whether a letter can be pinned to this panel at all.
 *
 *  Only a panel that is about one symbol can follow a group. A screener,
 *  a watchlist, a heat map and a comparison all carry their own list --
 *  `COMP` keeps its symbols in `?symbols=`, so a letter there would move
 *  the strip and leave the chart alone, which is a badge that lies.
 *
 *  Read from `needsSymbol`, the panel's own declaration, rather than
 *  from a list of codes kept here: a second list drifts the first time a
 *  panel changes its mind. */
export function canJoinGroup(spec: PanelSpec | undefined): boolean {
  return spec !== undefined && spec.needsSymbol;
}

/** The symbol a panel shows: its group's, or its own when it has none. */
export function symbolFor(
  groups: GroupSymbols,
  group: Group | null,
  own: string | null,
): string | null {
  return group === null ? own : (groups[group] ?? null);
}
