import type { ComponentType } from "react";

export type PanelArgs = Record<string, string>;

export interface PanelProps {
  symbol: string | null;
  args: PanelArgs;
}

export interface PanelSpec {
  code: string;
  title: string;
  needsSymbol: boolean;
  /** The argument syntax, for HELP: `FA [income|balance|cash] [annual|quarterly|ttm]`. */
  usage?: string;
  layout: "single" | "headed";
  /** Turns the raw tokens after the code into args. Throws Error(message) on bad input. */
  parseArgs(tokens: string[]): PanelArgs;
  component: ComponentType<PanelProps>;
}

export interface Command {
  symbol: string | null;
  code: string;
  args: PanelArgs;
}
