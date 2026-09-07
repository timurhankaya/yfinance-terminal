import { registerPanel } from "../commands/registry";
import { ANR_PANEL } from "./ANR";
import { CA_PANEL } from "./CA";
import { CF_PANEL } from "./CF";
import { CURATED } from "./curated";
import { DES_PANEL } from "./DES";
import { DS_PANEL } from "./DS";
import { EQS_PANEL } from "./EQS";
import { FA_PANEL } from "./FA";
import { GIP_PANEL } from "./GIP";
import { GP_PANEL } from "./GP";
import { HELP_PANEL } from "./HELP";
import { N_PANEL } from "./N";
import { PX_PANEL } from "./PX";
import { QR_PANEL } from "./QR";

export function registerAll(): void {
  registerPanel(ANR_PANEL);
  registerPanel(CA_PANEL);
  registerPanel(CF_PANEL);
  registerPanel(DES_PANEL);
  registerPanel(DS_PANEL);
  registerPanel(EQS_PANEL);
  registerPanel(FA_PANEL);
  registerPanel(GIP_PANEL);
  registerPanel(GP_PANEL);
  registerPanel(HELP_PANEL);
  registerPanel(N_PANEL);
  registerPanel(PX_PANEL);
  registerPanel(QR_PANEL);
  for (const panel of CURATED) registerPanel(panel);
}
