import { describe, expect, it } from "vitest";
import { CHANNEL_PREFIX, GENERATED_FIELDS, TICK_WIRE, WireKind, socketUrl } from "./types";

describe("the wire shape agrees with Python", () => {
  it("has exactly the keys the field table declares", () => {
    // `scripts/dump_tick_fields.py` writes that table from
    // `stream/publish.py`. A key renamed on one side only would arrive
    // here as `undefined` on a chart that still drew.
    expect(Object.keys(TICK_WIRE).sort()).toEqual(
      GENERATED_FIELDS.map((field) => field.key).sort(),
    );
  });

  it("encodes each key the same way", () => {
    const generated = Object.fromEntries(
      GENERATED_FIELDS.map((field) => [field.key, field.kind]),
    );
    expect(TICK_WIRE).toEqual(generated);
  });

  it("keeps the four fields a price cannot be drawn without", () => {
    const required = GENERATED_FIELDS.filter((field) => field.required).map((f) => f.key);
    expect(required.sort()).toEqual(["mh", "p", "s", "t"]);
  });

  it("sends timestamps as epoch milliseconds, not as text", () => {
    expect(TICK_WIRE.t).toBe(WireKind.Ms);
  });

  it("sends every price as a string", () => {
    // NUMERIC(28,12) does not survive binary64; a JSON number would
    // round it on the way in.
    for (const key of ["p", "c", "cp", "h", "l", "o", "pc", "b", "a"] as const) {
      expect(TICK_WIRE[key]).toBe(WireKind.Dec);
    }
  });

  it("names channels the way the publisher does", () => {
    expect(CHANNEL_PREFIX).toBe("yfin:tick:");
  });
});

describe("socketUrl", () => {
  it("follows the page's scheme", () => {
    // A page on https cannot open ws:, and a dev page on http cannot
    // use wss: -- either mismatch is a socket that never connects.
    expect(socketUrl({ protocol: "https:", host: "yfin.example" })).toBe(
      "wss://yfin.example/ui/ws",
    );
    expect(socketUrl({ protocol: "http:", host: "localhost:5173" })).toBe(
      "ws://localhost:5173/ui/ws",
    );
  });
});
