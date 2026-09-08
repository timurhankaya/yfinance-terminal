import type { ComponentType } from "react";

export type PanelArgs = Record<string, string>;

export interface PanelProps {
  symbol: string | null;
  args: PanelArgs;
}

/** How the shell frames a panel: one body, or a head above its body. */
export enum Layout {
  Single = "single",
  Headed = "headed",
}

export interface PanelSpec {
  code: string;
  title: string;
  needsSymbol: boolean;
  /** The argument syntax, for HELP: `FA [income|balance|cash] [annual|quarterly|ttm]`. */
  usage?: string;
  layout: Layout;
  /** Turns the raw tokens after the code into args. Throws Error(message) on bad input. */
  parseArgs(tokens: string[]): PanelArgs;
  /** Brings args that did NOT come through `parseArgs` back into range.
   *
   *  A bookmarked or hand-edited URL reaches a panel without ever being
   *  parsed, so the same rules have to be applied once more. The shell
   *  calls this after reading the URL, which is what keeps every panel
   *  behaving the same on a bad `?interval=` -- rather than half of them
   *  clamping locally and half passing it to the API. */
  normalizeArgs?(args: PanelArgs): PanelArgs;
  component: ComponentType<PanelProps>;
}

/** A command that changes the page rather than filling a panel:
 *  `GRP B` pins the focused panel to a letter. It has a code and a usage
 *  line like a panel, and nothing else -- there is nothing to render. */
export interface ActionSpec {
  code: string;
  title: string;
  usage?: string;
}

export interface Command {
  symbol: string | null;
  code: string;
  args: PanelArgs;
}
