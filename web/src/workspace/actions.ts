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

export const ACTIONS: readonly ActionSpec[] = [GRP_ACTION];
