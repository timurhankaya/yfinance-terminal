import { registerPanel } from "../commands/registry";
import { DES_PANEL } from "./DES";
import { HELP_PANEL } from "./HELP";

export function registerAll(): void {
  registerPanel(DES_PANEL);
  registerPanel(HELP_PANEL);
}
