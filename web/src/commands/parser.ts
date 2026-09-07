// The command grammar: [SYMBOL] [CODE] [ARGS...]. Deterministic on purpose —
// no fuzzy matching, no guessing. Symbols and codes are compared upper-case;
// argument tokens reach parseArgs exactly as typed, because "15m" is an
// interval and "15M" is not. The one ambiguity (a ticker that is also a
// mnemonic, like CF) is settled by position: two leading mnemonics mean the
// first is the symbol.
import { getPanel, isMnemonic } from "./registry";
import type { Command, PanelArgs } from "./types";

export const SYMBOL_RE = /^[A-Z0-9.^=-]+$/;
/** The URL segment that stands for "no symbol": /ui/t/-/HELP. */
export const NO_SYMBOL = "-";
export const DEFAULT_CODE = "DES";

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

export function commandToPath(command: Command): string {
  const symbol = command.symbol === null ? NO_SYMBOL : encodeURIComponent(command.symbol);
  const query = new URLSearchParams(command.args).toString();
  return `/ui/t/${symbol}/${command.code}${query ? `?${query}` : ""}`;
}

/** `symbol` and `code` are the path segments as react-router hands them over: already decoded. */
export function pathToCommand(symbol: string, code: string, search: string): Command {
  const args: PanelArgs = {};
  new URLSearchParams(search).forEach((value, key) => {
    args[key] = value;
  });
  return { symbol: symbol === NO_SYMBOL ? null : symbol.toUpperCase(), code: code.toUpperCase(), args };
}
