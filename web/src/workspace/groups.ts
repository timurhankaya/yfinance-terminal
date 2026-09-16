// Which symbol a panel is about, when a page has more than one. A letter
// belongs to the panel, not to the box it sits in: dragging a panel does
// not change what it is watching. The letters and colours are
// `viz/colors`, the same seven identities the comparison chart uses.
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

/** Whether a letter can be pinned to this panel at all. Only a panel
 *  about one symbol can follow a group: `COMP` keeps its symbols in
 *  `?symbols=`, so a letter there would move the strip and leave the
 *  chart alone. Read from `needsSymbol` rather than a list of codes here. */
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
