import { registerPanel } from "../commands/registry";
import { ANR_PANEL } from "./ANR";
import { CA_PANEL } from "./CA";
import { CF_PANEL } from "./CF";
import { CURATED } from "./curated";
import { DES_PANEL } from "./DES";
import { DS_PANEL } from "./DS";
import { FA_PANEL } from "./FA";
import { HELP_PANEL } from "./HELP";
import { N_PANEL } from "./N";
import { PX_PANEL } from "./PX";

export function registerAll(): void {
  registerPanel(ANR_PANEL);
  registerPanel(CA_PANEL);
  registerPanel(CF_PANEL);
  registerPanel(DES_PANEL);
  registerPanel(DS_PANEL);
  registerPanel(FA_PANEL);
  registerPanel(HELP_PANEL);
  registerPanel(N_PANEL);
  registerPanel(PX_PANEL);
  for (const panel of CURATED) registerPanel(panel);
}
