// The command grammar: [SYMBOL] [CODE] [ARGS...]. Deterministic on purpose —
// no fuzzy matching, no guessing. Symbols and codes are compared upper-case;
// argument tokens reach parseArgs exactly as typed, because "15m" is an
// interval and "15M" is not. The one ambiguity (a ticker that is also a
// mnemonic, like CF) is settled by position: two leading mnemonics mean the
// first is the symbol.
import { getPanel, isMnemonic } from "./registry";
import type { Command, PanelArgs } from "./types";

export const SYMBOL_RE = /^[A-Z0-9.^=-]+$/;
/** The URL segment that stands for "no symbol" on a symbol route:
 *  `/ui/t/-/FA` is a hand-typed URL for a panel that needs one and has
 *  not been given it, and the shell says so rather than 404ing. */
export const NO_SYMBOL = "-";
export const DEFAULT_CODE = "DES";

/** The two roots, and why there are two.
 *
 *  A screener is not a property of a symbol. Serving `EQS` from
 *  `/ui/t/AAPL/EQS` made every market-wide page read as though it
 *  belonged to whatever symbol happened to be on the strip, and made
 *  `/ui/t/-/EQS` -- a placeholder standing in for a slot the page has no
 *  use for -- the shape of a shareable link.
 *
 *  So the URL now says which kind of page it is. `/ui/t/{SYMBOL}/{CODE}`
 *  is a symbol's detail, where the symbol is part of the identity;
 *  `/ui/m/{CODE}` is a market-wide page, where it is not.
 *
 *  The strip's symbol still follows the reader across a market page --
 *  `AAPL DES` then `EQS` then `FA` lands back on AAPL -- but it rides in
 *  the history entry rather than the path (`Shell`). That is what makes
 *  a shared `/ui/m/EQS` carry no one's symbol, which is the correct
 *  thing for it to carry. */
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
  Empty = "empty",
  Error = "error",
}

export type ParseResult =
  | { kind: ParseKind.Command; command: Command }
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

  if (raw.length === 1) {
    if (isMnemonic(first)) return build(ctx.symbol, first, []);
    if (!SYMBOL_RE.test(first)) return { kind: ParseKind.Error, message: `${first} is not a symbol` };
    // A bare symbol keeps the current panel, unless that panel takes no symbol.
    const current = getPanel(ctx.code);
    const code = current && current.needsSymbol ? current.code : DEFAULT_CODE;
    return build(first, code, []);
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

/** True when the code names a page that is not about one symbol.
 *
 *  Read from the registry rather than from a list here: `needsSymbol` is
 *  already the panel's own declaration, and a second list would drift
 *  the first time a panel changed its mind. An unregistered code is
 *  treated as symbol-scoped, which is what `DES` -- the default -- is. */
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
