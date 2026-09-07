import { registerPanel } from "../commands/registry";
import { ANR_PANEL } from "./ANR";
import { CF_PANEL } from "./CF";
import { DES_PANEL } from "./DES";
import { FA_PANEL } from "./FA";
import { HELP_PANEL } from "./HELP";
import { N_PANEL } from "./N";

export function registerAll(): void {
  registerPanel(ANR_PANEL);
  registerPanel(CF_PANEL);
  registerPanel(DES_PANEL);
  registerPanel(FA_PANEL);
  registerPanel(HELP_PANEL);
  registerPanel(N_PANEL);
}
