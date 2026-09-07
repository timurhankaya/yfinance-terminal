// The live wire, typed. The field table itself is Python's
// (`src/yfin/stream/publish.py`) and reaches this directory as the
// generated `tick-fields.json`; what is written by hand here is only the
// TypeScript shape, and a test holds the two together so a renamed key
// breaks the build instead of a chart.
import table from "./tick-fields.json";

/** Every frame carries one of these under `op`, both directions. */
export enum Op {
  Sub = "sub",
  Unsub = "unsub",
  Live = "live",
  Snap = "snap",
  Tick = "tick",
  Dropped = "dropped",
  Error = "error",
}

/** What the server refuses a frame for. */
export enum WsErrorCode {
  TooMany = "too_many",
  BadSymbol = "bad_symbol",
}

/** Where the socket is, for the strip's indicator. */
export enum LinkState {
  Connecting = "connecting",
  Open = "open",
  Closed = "closed",
}

/** Yahoo's market_hours codes, as `mh` carries them.
 *
 *  The chart needs these by name: a `session=regular` series must drop
 *  pre- and post-market ticks, and comparing against a bare 1 in three
 *  places is how that rule drifts. Names and numbers are Yahoo's
 *  (`stream/protocol.py` keeps the same mapping on the Python side). */
export enum MarketHours {
  PreMarket = 0,
  Regular = 1,
  PostMarket = 2,
  ExtendedHours = 3,
}

/** One tick, exactly as `publish.py` sends it.
 *
 *  Short keys because a body goes out per tick per subscriber; the
 *  generated table is where each one is spelled out. Decimals arrive as
 *  STRINGS: these are NUMERIC(28,12) in the archive and a JSON number
 *  would round them here. */
export interface Tick {
  /** symbol */
  s: string;
  /** bar open, epoch milliseconds UTC */
  t: number;
  /** price */
  p: string;
  /** market_hours_code */
  mh: number;
  /** change */
  c?: string;
  /** change_percent */
  cp?: string;
  /** day_high */
  h?: string;
  /** day_low */
  l?: string;
  /** open_price */
  o?: string;
  /** previous_close */
  pc?: string;
  /** bid */
  b?: string;
  /** ask */
  a?: string;
  /** day_volume */
  v?: number;
  /** last_size */
  ls?: number;
  /** bid_size */
  bs?: number;
  /** ask_size */
  as?: number;
}

/** How each key is encoded, mirroring `publish.FieldKind`. */
export enum WireKind {
  Str = "str",
  Int = "int",
  Ms = "ms",
  Dec = "dec",
}

/** Every wire key with its encoding, as a value.
 *
 *  Two fences meet here. `Record<keyof Tick, WireKind>` is the compile-time
 *  one: a key added to `Tick` and not to this map, or the other way
 *  round, stops type-checking. `tick-fields.json` is the runtime one, and
 *  `types.test.ts` compares the two -- so a key renamed in Python breaks
 *  a test rather than silently arriving as `undefined` on a chart. */
export const TICK_WIRE: Record<keyof Tick, WireKind> = {
  s: WireKind.Str,
  t: WireKind.Ms,
  p: WireKind.Dec,
  mh: WireKind.Int,
  c: WireKind.Dec,
  cp: WireKind.Dec,
  h: WireKind.Dec,
  l: WireKind.Dec,
  o: WireKind.Dec,
  pc: WireKind.Dec,
  b: WireKind.Dec,
  a: WireKind.Dec,
  v: WireKind.Int,
  ls: WireKind.Int,
  bs: WireKind.Int,
  as: WireKind.Int,
};

/** The generated table, as the tests read it. */
export const GENERATED_FIELDS: ReadonlyArray<{
  key: string;
  column: string;
  kind: string;
  required: boolean;
}> = table.fields;

export const CHANNEL_PREFIX: string = table.channelPrefix;

export type ServerFrame =
  | { op: Op.Live; enabled: boolean }
  | { op: Op.Snap; d: Tick }
  | { op: Op.Tick; d: Tick }
  | { op: Op.Dropped; n: number }
  | { op: Op.Error; code: WsErrorCode };

export type ClientFrame =
  | { op: Op.Sub; symbols: string[] }
  | { op: Op.Unsub; symbols: string[] };

/** The socket's own address. Same origin, so the scheme follows the page's:
 *  a page on https must not open ws:, and a dev page on http cannot use wss. */
export function socketUrl(location: { protocol: string; host: string }): string {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/ui/ws`;
}
