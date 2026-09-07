import { beforeEach, describe, expect, it } from "vitest";
import { clearRegistry, registerPanel } from "./registry";
import { commandToPath, parse, pathToCommand } from "./parser";
import type { PanelArgs, PanelSpec } from "./types";

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
  return { code, title: code, needsSymbol, layout: "single", parseArgs, component: Noop };
}

const INTERVALS = ["1m", "5m", "15m", "60m"];

beforeEach(() => {
  clearRegistry();
  registerPanel(spec("DES"));
  registerPanel(spec("FA"));
  registerPanel(spec("CF"));
  registerPanel(spec("HELP", false, () => ({})));
  registerPanel(spec("GIP", true, (t) => {
    const interval = (t[0] ?? "5m").toLowerCase();
    if (!INTERVALS.includes(interval)) throw new Error(`Unknown interval ${t[0]}`);
    return { interval };
  }));
});

const ctx = { symbol: "AAPL", code: "DES" };

describe("parse", () => {
  it.each([
    ["", { kind: "empty" }],
    ["   ", { kind: "empty" }],
    ["msft", { kind: "command", command: { symbol: "MSFT", code: "DES", args: {} } }],
    ["FA", { kind: "command", command: { symbol: "AAPL", code: "FA", args: {} } }],
    ["msft fa", { kind: "command", command: { symbol: "MSFT", code: "FA", args: {} } }],
    ["CF DES", { kind: "command", command: { symbol: "CF", code: "DES", args: {} } }],
    ["cf", { kind: "command", command: { symbol: "AAPL", code: "CF", args: {} } }],
    ["GIP 15m", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "15m" } } }],
    ["GIP 15M", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "15m" } } }],
    ["AAPL GIP 1m", { kind: "command", command: { symbol: "AAPL", code: "GIP", args: { interval: "1m" } } }],
    ["HELP", { kind: "command", command: { symbol: null, code: "HELP", args: {} } }],
    ["BRK-B", { kind: "command", command: { symbol: "BRK-B", code: "DES", args: {} } }],
    ["^GSPC DES", { kind: "command", command: { symbol: "^GSPC", code: "DES", args: {} } }],
    ["FA DES x", { kind: "command", command: { symbol: "FA", code: "DES", args: { a: "x" } } }],
  ])("%j", (input, expected) => {
    expect(parse(input, ctx)).toEqual(expected);
  });

  it("keeps the panel when only a symbol is typed, unless the panel needs no symbol", () => {
    expect(parse("MSFT", { symbol: null, code: "HELP" })).toEqual({
      kind: "command", command: { symbol: "MSFT", code: "DES", args: {} },
    });
    expect(parse("MSFT", { symbol: "AAPL", code: "FA" })).toEqual({
      kind: "command", command: { symbol: "MSFT", code: "FA", args: {} },
    });
  });

  it("refuses a function that needs a symbol when there is none", () => {
    expect(parse("FA", { symbol: null, code: "HELP" })).toEqual({
      kind: "error", message: "FA needs a symbol: type one first, e.g. AAPL FA",
    });
  });

  it("rejects an invalid symbol token and an unknown function", () => {
    expect(parse("A$B", ctx)).toEqual({ kind: "error", message: "A$B is not a symbol" });
    expect(parse("AAPL XYZ", ctx)).toEqual({ kind: "error", message: "Unknown function XYZ" });
    expect(parse("XYZ", { symbol: null, code: "HELP" })).toEqual({
      kind: "command", command: { symbol: "XYZ", code: "DES", args: {} },
    });
  });

  it("surfaces parseArgs errors without a command, keeping the user's spelling", () => {
    expect(parse("GIP 3m", ctx)).toEqual({ kind: "error", message: "Unknown interval 3m" });
  });
});

describe("paths", () => {
  it("round-trips a command through the URL", () => {
    const cmd = { symbol: "AAPL", code: "GIP", args: { interval: "5m" } };
    expect(commandToPath(cmd)).toBe("/ui/t/AAPL/GIP?interval=5m");
    expect(pathToCommand("AAPL", "GIP", "?interval=5m")).toEqual(cmd);
  });
  it("uses '-' for no symbol and encodes odd symbols on the way out only", () => {
    expect(commandToPath({ symbol: null, code: "HELP", args: {} })).toBe("/ui/t/-/HELP");
    expect(commandToPath({ symbol: "^GSPC", code: "DES", args: {} })).toBe("/ui/t/%5EGSPC/DES");
    // useParams hands us the decoded segment, so no second decode here.
    expect(pathToCommand("-", "HELP", "")).toEqual({ symbol: null, code: "HELP", args: {} });
    expect(pathToCommand("^gspc", "des", "")).toEqual({ symbol: "^GSPC", code: "DES", args: {} });
  });
});
