import type { ActionSpec, PanelSpec } from "./types";

// Two kinds of thing can be typed into the command box, and only one of
// them is a panel. `GRP` pins a panel to a letter and draws nothing; if
// it were registered as a panel to make the parser accept it, it would
// appear in the function bar, in the palette and in HELP's list of
// pages, none of which it is.

const panels = new Map<string, PanelSpec>();
const actions = new Map<string, ActionSpec>();

export function registerPanel(spec: PanelSpec): void {
  panels.set(spec.code.toUpperCase(), spec);
}

export function registerAction(spec: ActionSpec): void {
  actions.set(spec.code.toUpperCase(), spec);
}

export function getPanel(code: string): PanelSpec | undefined {
  return panels.get(code.toUpperCase());
}

export function getAction(code: string): ActionSpec | undefined {
  return actions.get(code.toUpperCase());
}

export function listActions(): ActionSpec[] {
  return [...actions.values()].sort((a, b) => a.code.localeCompare(b.code));
}

export function listPanels(): PanelSpec[] {
  return [...panels.values()].sort((a, b) => a.code.localeCompare(b.code));
}

/** Whether the token is a command of either kind -- which is what the
 *  grammar needs to know to stop reading it as a ticker. */
export function isMnemonic(token: string): boolean {
  const code = token.toUpperCase();
  return panels.has(code) || actions.has(code);
}

/** Codes that were retired, and where their work went. Not an alias: a
 *  saved page or a pasted link may still carry the old code, and this
 *  turns "Unknown function" into a direction. */
export const RETIRED: Readonly<Record<string, string>> = {
  GIP: "GP 5m — one chart function now takes any interval",
  SCR: "EQS for screens; DS screens / screen_runs / screen_members / screen_quotes for the raw tables",
  SRCH: "DS search_quotes / search_lists / lookup_results for the archived results",
};

/** Where a retired code's work went, or undefined for a code that never
 *  existed. */
export function retiredNote(code: string): string | undefined {
  return RETIRED[code.toUpperCase()];
}
