// Commands that change the page rather than fill a panel.
//
// `GRP` pins the focused panel to a letter, so `AAPL` typed once moves
// every panel wearing it. It is not a panel: it draws nothing, and it
// has no business in the function bar or in HELP's list of pages.
import type { ActionSpec } from "../commands/types";

export const GRP_CODE = "GRP";
/** `GRP -`: the token that takes a letter away rather than naming one. */
export const GROUP_DETACH = "-";

export const GRP_ACTION: ActionSpec = {
  code: GRP_CODE,
  title: "Pin this panel to a group letter",
  usage: `GRP [A-G|${GROUP_DETACH}]`,
};

export const PG_CODE = "PG";
/** `PG SAVE <name>`: the one form that writes rather than opens. */
export const PG_SAVE = "SAVE";

/** `PG` is BOTH an action and a panel. The panel is the page manager;
 *  `PG SAVE trading` and `PG trading` write the store or change which
 *  page the window is on, which a panel's `parseArgs` cannot do from
 *  inside the layout it is saving. The parser resolves actions first, so
 *  every typed `PG` arrives here; the shell opens the panel for the bare form. */
export const PG_ACTION: ActionSpec = {
  code: PG_CODE,
  title: "Saved pages: list them, open one, or name this one",
  usage: `PG [${PG_SAVE} <name>|<name>]`,
};

export const SHARE_CODE = "SHARE";

export const SHARE_ACTION: ActionSpec = {
  code: SHARE_CODE,
  title: "Write this page as a link",
  usage: "SHARE",
};

export const ACTIONS: readonly ActionSpec[] = [GRP_ACTION, PG_ACTION, SHARE_ACTION];
