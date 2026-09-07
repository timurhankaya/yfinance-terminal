import { registerPanel } from "../commands/registry";
import { ANR_PANEL } from "./ANR";
import { DES_PANEL } from "./DES";
import { FA_PANEL } from "./FA";
import { HELP_PANEL } from "./HELP";

export function registerAll(): void {
  registerPanel(ANR_PANEL);
  registerPanel(DES_PANEL);
  registerPanel(FA_PANEL);
  registerPanel(HELP_PANEL);
}
