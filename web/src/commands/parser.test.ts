import { describe, expect, it } from "vitest";
import { registerAction, registerPanel } from "./registry";
import { commandToPath, isMarketCode, parse, ParseKind, pathToCommand, symbolCommand } from "./parser";
import { Layout, type PanelArgs, type PanelSpec } from "./types";

const Noop = () => null;

function joined(t: string[]): PanelArgs {
  const args: PanelArgs = {};
  if (t.length > 0) args.a = t.join(" ");
  return args;
}

function spec(
  code: string,
  needsSymbol = true,
  parseArgs: (t: string[]) => PanelArgs = joined,
): PanelSpec {
  return { code, title: code, needsSymbol, layout: Layout.Single, parseArgs, component: Noop };
}

const INTERVALS = ["1m", "5m", "15m", "60m"];

registerPanel(spec("DES"));
registerPanel(spec("FA"));
registerPanel(spec("CF"));
registerPanel(spec("HELP", false, () => ({})));
registerPanel(spec("GIP", true, (t) => {
  const interval = (t[0] ?? "5m").toLowerCase();
  if (!INTERVALS.includes(interval)) throw new Error(`Unknown interval ${t[0]}`);
  return { interval };
}));
registerAction({ code: "GRP", title: "group" });

const ctx = { symbol: "AAPL", code: "DES" };

describe("parse", () => {
  it.each([
    ["", { kind: ParseKind.Empty }],
    ["   ", { kind: ParseKind.Empty }],
    // A bare ticker is not a command yet: on a page with groups it moves
    // a letter, and the shell asks `symbolCommand` what it means where
    // there are none.
    ["msft", { kind: ParseKind.Symbol, symbol: "MSFT" }],
    ["FA", { kind: ParseKind.Command, command: { symbol: "AAPL", code: "FA", args: {} } }],
    ["msft fa", { kind: ParseKind.Command, command: { symbol: "MSFT", code: "FA", args: {} } }],
    ["CF DES", { kind: ParseKind.Command, command: { symbol: "CF", code: "DES", args: {} } }],
    ["cf", { kind: ParseKind.Command, command: { symbol: "AAPL", code: "CF", args: {} } }],
    ["GIP 15m", { kind: ParseKind.Command, command: { symbol: "AAPL", code: "GIP", args: { interval: "15m" } } }],
    ["GIP 15M", { kind: ParseKind.Command, command: { symbol: "AAPL", code: "GIP", args: { interval: "15m" } } }],
    ["AAPL GIP 1m", { kind: ParseKind.Command, command: { symbol: "AAPL", code: "GIP", args: { interval: "1m" } } }],
    // A symbol-less panel still carries the context symbol, so the next
    // "FA" after HELP has something to run against.
    ["HELP", { kind: ParseKind.Command, command: { symbol: "AAPL", code: "HELP", args: {} } }],
    ["BRK-B", { kind: ParseKind.Symbol, symbol: "BRK-B" }],
    ["^GSPC DES", { kind: ParseKind.Command, command: { symbol: "^GSPC", code: "DES", args: {} } }],
    ["FA DES x", { kind: ParseKind.Command, command: { symbol: "FA", code: "DES", args: { a: "x" } } }],
  ])("%j", (input, expected) => {
    expect(parse(input, ctx)).toEqual(expected);
  });

  it("keeps the panel when only a symbol is typed, unless the panel needs no symbol", () => {
    // `parse` reports the ticker; `symbolCommand` is where "which panel"
    // is decided, so that a group can take the same input somewhere else.
    expect(symbolCommand("MSFT", "HELP")).toEqual({
      kind: ParseKind.Command, command: { symbol: "MSFT", code: "DES", args: {} },
    });
    expect(symbolCommand("MSFT", "FA")).toEqual({
      kind: ParseKind.Command, command: { symbol: "MSFT", code: "FA", args: {} },
    });
  });

  it("reads a group command as an action, not as a ticker", () => {
    expect(parse("GRP B", ctx)).toEqual({ kind: ParseKind.Action, code: "GRP", tokens: ["B"] });
    expect(parse("GRP", ctx)).toEqual({ kind: ParseKind.Action, code: "GRP", tokens: [] });
  });

  it("refuses a function that needs a symbol when there is none", () => {
    expect(parse("FA", { symbol: null, code: "HELP" })).toEqual({
      kind: ParseKind.Error, message: "FA needs a symbol: type one first, e.g. AAPL FA",
    });
  });

  it("rejects an invalid symbol token and an unknown function", () => {
    expect(parse("A$B", ctx)).toEqual({ kind: ParseKind.Error, message: "A$B is not a symbol" });
    expect(parse("AAPL XYZ", ctx)).toEqual({ kind: ParseKind.Error, message: "Unknown function XYZ" });
    expect(parse("XYZ", { symbol: null, code: "HELP" })).toEqual({
      kind: ParseKind.Symbol, symbol: "XYZ",
    });
  });

  it("surfaces parseArgs errors without a command, keeping the user's spelling", () => {
    expect(parse("GIP 3m", ctx)).toEqual({ kind: ParseKind.Error, message: "Unknown interval 3m" });
  });
});

describe("paths", () => {
  it("round-trips a command through the URL", () => {
    const cmd = { symbol: "AAPL", code: "GIP", args: { interval: "5m" } };
    expect(commandToPath(cmd)).toBe("/ui/t/AAPL/GIP?interval=5m");
    expect(pathToCommand("AAPL", "GIP", "?interval=5m")).toEqual(cmd);
  });
  it("puts a market-wide page on its own root, with no symbol slot", () => {
    // A screener is not a property of a symbol, and `/ui/t/AAPL/HELP`
    // said it was. The strip's symbol still travels with the reader --
    // in the history entry (`useGo`), not in the address.
    expect(commandToPath({ symbol: null, code: "HELP", args: {} })).toBe("/ui/m/HELP");
    expect(commandToPath({ symbol: "AAPL", code: "HELP", args: {} })).toBe("/ui/m/HELP");
    // Args ride along as they always did; only the symbol slot is gone.
    expect(commandToPath({ symbol: "AAPL", code: "HELP", args: { a: "1,2" } })).toBe(
      "/ui/m/HELP?a=1%2C2",
    );
  });

  it("keeps the symbol in the address of a symbol's own page", () => {
    expect(commandToPath({ symbol: "^GSPC", code: "DES", args: {} })).toBe("/ui/t/%5EGSPC/DES");
    expect(commandToPath({ symbol: null, code: "DES", args: {} })).toBe("/ui/t/-/DES");
  });

  it("gives the home the market root itself", () => {
    // `/ui` is what a reader types and what a bookmark holds; a code
    // under the market root would be a second address for one page.
    expect(commandToPath({ symbol: null, code: "HOME", args: {} })).toBe("/ui");
    expect(commandToPath({ symbol: "AAPL", code: "HOME", args: {} })).toBe("/ui");
  });

  it("reads a symbol back out of a path, and null off a market route", () => {
    // useParams hands us the decoded segment, so no second decode here.
    expect(pathToCommand("-", "FA", "")).toEqual({ symbol: null, code: "FA", args: {} });
    expect(pathToCommand("^gspc", "des", "")).toEqual({ symbol: "^GSPC", code: "DES", args: {} });
    expect(pathToCommand(null, "eqs", "")).toEqual({ symbol: null, code: "EQS", args: {} });
  });

  it("calls a code market-wide only when the panel says it needs no symbol", () => {
    // Read from the registry rather than a list here, so the two cannot
    // disagree about what a panel is.
    expect(isMarketCode("HELP")).toBe(true);
    expect(isMarketCode("DES")).toBe(false);
    // An unregistered code is treated as symbol-scoped, which is what
    // the default panel is.
    expect(isMarketCode("NOPE")).toBe(false);
  });
});
