// The command grammar: [SYMBOL] [CODE] [ARGS...]. Deterministic: no fuzzy
// matching. Symbols and codes compare upper-case; argument tokens reach
// parseArgs exactly as typed ("15m" is an interval and "15M" is not). A
// ticker that is also a mnemonic (CF) is settled by position: two leading
// mnemonics mean the first is the symbol.
import { getAction, getPanel, isMnemonic } from "./registry";
import type { Command, PanelArgs } from "./types";

export const SYMBOL_RE = /^[A-Z0-9.^=-]+$/;
/** The URL segment that stands for "no symbol" on a symbol route:
 *  `/ui/t/-/FA` is a hand-typed URL for a panel that needs one and has
 *  not been given it, and the shell says so rather than 404ing. */
export const NO_SYMBOL = "-";
export const DEFAULT_CODE = "DES";

/** The two roots. `/ui/t/{SYMBOL}/{CODE}` is a symbol's detail, where the
 *  symbol is part of the identity; `/ui/m/{CODE}` is a market-wide page,
 *  where it is not. The strip's symbol follows the reader across a market
 *  page in the history entry (`Shell`), so a shared `/ui/m/EQS` carries
 *  no one's symbol. */
export const SYMBOL_ROOT = "/ui/t";
export const MARKET_ROOT = "/ui/m";

/** The market root itself. `HOME` is the one page whose path is a root
 *  rather than a code under one: `/ui` is what a reader types and what a
 *  bookmark holds. */
export const HOME_CODE = "HOME";
export const HOME_PATH = "/ui";

export interface ParseContext {
  symbol: string | null;
  code: string;
}

export enum ParseKind {
  Command = "command",
  /** A bare ticker. Not a command yet: on a page with groups it moves a
   *  letter, and only where there are none does it mean "this panel, that
   *  symbol" (`symbolCommand`). The shell knows which; the grammar does
   *  not. */
  Symbol = "symbol",
  /** `GRP B`: changes the page, fills no panel. */
  Action = "action",
  Empty = "empty",
  Error = "error",
}

export type ParseResult =
  | { kind: ParseKind.Command; command: Command }
  | { kind: ParseKind.Symbol; symbol: string }
  | { kind: ParseKind.Action; code: string; tokens: string[] }
  | { kind: ParseKind.Empty }
  | { kind: ParseKind.Error; message: string };

function build(symbol: string | null, code: string, rawArgs: string[]): ParseResult {
  const spec = getPanel(code);
  if (!spec) return { kind: ParseKind.Error, message: `Unknown function ${code}` };
  if (spec.needsSymbol && symbol === null) {
    return { kind: ParseKind.Error, message: `${code} needs a symbol: type one first, e.g. AAPL ${code}` };
  }
  let args: PanelArgs;
  try {
    args = spec.parseArgs(rawArgs);
  } catch (err) {
    return { kind: ParseKind.Error, message: err instanceof Error ? err.message : String(err) };
  }
  // The context symbol rides along even into panels that do not need it
  // (HELP): otherwise "AAPL DES, HELP, FA" would forget AAPL half-way.
  return { kind: ParseKind.Command, command: { symbol, code: spec.code, args } };
}

export function parse(input: string, ctx: ParseContext): ParseResult {
  const raw = input.trim().split(/\s+/).filter(Boolean);
  if (raw.length === 0) return { kind: ParseKind.Empty };
  const upper = raw.map((t) => t.toUpperCase());
  const first = upper[0] ?? "";
  const second = upper[1];

  if (getAction(first) !== undefined) {
    return { kind: ParseKind.Action, code: first, tokens: raw.slice(1) };
  }

  if (raw.length === 1) {
    if (isMnemonic(first)) return build(ctx.symbol, first, []);
    if (!SYMBOL_RE.test(first)) return { kind: ParseKind.Error, message: `${first} is not a symbol` };
    return { kind: ParseKind.Symbol, symbol: first };
  }

  // Two or more tokens.
  if (second !== undefined && isMnemonic(first) && !isMnemonic(second)) {
    // "GIP 15m": function plus its arguments, on the context symbol.
    return build(ctx.symbol, first, raw.slice(1));
  }
  // "CF DES", "AAPL GIP 1m": symbol, function, arguments.
  if (!SYMBOL_RE.test(first)) return { kind: ParseKind.Error, message: `${first} is not a symbol` };
  if (second === undefined || !isMnemonic(second)) {
    return { kind: ParseKind.Error, message: `Unknown function ${second ?? ""}` };
  }
  return build(first, second, raw.slice(2));
}

/** What a bare ticker means where no letter claims the panel: keep the
 *  panel and change its symbol, unless that panel takes none. */
export function symbolCommand(symbol: string, ctxCode: string): ParseResult {
  const current = getPanel(ctxCode);
  const code = current && current.needsSymbol ? current.code : DEFAULT_CODE;
  return build(symbol, code, []);
}

/** True when the code names a page that is not about one symbol. Read
 *  from the registry's `needsSymbol` rather than a second list here. An
 *  unregistered code is treated as symbol-scoped, like `DES`. */
export function isMarketCode(code: string): boolean {
  const spec = getPanel(code);
  return spec !== undefined && !spec.needsSymbol;
}

export function commandToPath(command: Command): string {
  const query = new URLSearchParams(command.args).toString();
  const suffix = query ? `?${query}` : "";
  if (command.code === HOME_CODE) return `${HOME_PATH}${suffix}`;
  if (isMarketCode(command.code)) return `${MARKET_ROOT}/${command.code}${suffix}`;
  const symbol = command.symbol === null ? NO_SYMBOL : encodeURIComponent(command.symbol);
  return `${SYMBOL_ROOT}/${symbol}/${command.code}${suffix}`;
}

/** The command a path denotes.
 *
 *  `symbol` is the path segment react-router hands over (already
 *  decoded), or null on a market route -- where the page has no symbol
 *  of its own and the caller supplies the strip's context instead. */
export function pathToCommand(symbol: string | null, code: string, search: string): Command {
  const args: PanelArgs = {};
  new URLSearchParams(search).forEach((value, key) => {
    args[key] = value;
  });
  const named = symbol === null || symbol === NO_SYMBOL ? null : symbol.toUpperCase();
  return { symbol: named, code: code.toUpperCase(), args };
}
