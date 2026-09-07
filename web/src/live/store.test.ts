import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  TAPE_MAX,
  handleFrame,
  isRegularSession,
  release,
  resetLive,
  retain,
  setSocketFactory,
  setTape,
  useLive,
} from "./store";
import type { SocketLike } from "./socket";
import { MarketHours, Op } from "./types";
import type { Tick } from "./types";

class FakeSocket implements SocketLike {
  sent: string[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {}
}

let opened: FakeSocket[] = [];
let frames: Array<() => void> = [];

function sent(): unknown[] {
  return opened.flatMap((socket) => socket.sent.map((text) => JSON.parse(text) as unknown));
}

/** Runs whatever `requestAnimationFrame` was handed, once. */
function paint(): void {
  const queued = frames;
  frames = [];
  for (const callback of queued) callback();
}

function tick(overrides: Partial<Tick> = {}): Tick {
  return { s: "AAPL", t: 1_000, p: "232.35", mh: MarketHours.Regular, ...overrides };
}

beforeEach(() => {
  opened = [];
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => {
    frames.push(callback);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  setSocketFactory(() => {
    const socket = new FakeSocket();
    opened.push(socket);
    // The store's socket subscribes on open; the fake is open at once.
    queueMicrotask(() => socket.onopen?.({}));
    return socket;
  });
  resetLive();
});

afterEach(() => {
  resetLive();
  setSocketFactory(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("coalescing", () => {
  it("applies a burst in one paint", () => {
    // A market open ticks faster than the screen refreshes; without this
    // the terminal gets slower the more data it has.
    for (let index = 0; index < 50; index += 1) {
      handleFrame({ op: Op.Tick, d: tick({ t: 1_000 + index, p: String(index) }) });
    }
    expect(useLive.getState().quotes.AAPL).toBeUndefined();
    paint();
    expect(useLive.getState().quotes.AAPL?.p).toBe("49");
  });

  it("keeps the last tick per symbol, not the first", () => {
    handleFrame({ op: Op.Tick, d: tick({ t: 1, p: "1" }) });
    handleFrame({ op: Op.Tick, d: tick({ t: 2, p: "2" }) });
    handleFrame({ op: Op.Tick, d: tick({ s: "MSFT", t: 2, p: "501" }) });
    paint();
    const { quotes } = useLive.getState();
    expect(quotes.AAPL?.p).toBe("2");
    expect(quotes.MSFT?.p).toBe("501");
  });

  it("schedules nothing when nothing arrived", () => {
    paint();
    expect(useLive.getState().quotes).toEqual({});
  });
});

describe("the snapshot floor", () => {
  it("drops a tick that was in flight when the snapshot was read", () => {
    // `sub` subscribes before it snapshots, so a tick older than the
    // snapshot is one the snapshot already includes.
    handleFrame({ op: Op.Snap, d: tick({ t: 5_000, p: "232.50" }) });
    handleFrame({ op: Op.Tick, d: tick({ t: 4_999, p: "1.00" }) });
    paint();
    expect(useLive.getState().quotes.AAPL?.p).toBe("232.50");
  });

  it("keeps a tick from the same millisecond as the snapshot", () => {
    // Yahoo sends several messages inside one millisecond; the archive's
    // primary key is a triple for exactly that reason.
    handleFrame({ op: Op.Snap, d: tick({ t: 5_000, p: "232.50" }) });
    handleFrame({ op: Op.Tick, d: tick({ t: 5_000, p: "232.60" }) });
    paint();
    expect(useLive.getState().quotes.AAPL?.p).toBe("232.60");
  });
});

describe("the tape", () => {
  beforeEach(() => {
    setTape("AAPL");
  });

  it("is newest first", () => {
    handleFrame({ op: Op.Tick, d: tick({ t: 1, p: "1" }) });
    handleFrame({ op: Op.Tick, d: tick({ t: 2, p: "2" }) });
    paint();
    expect(useLive.getState().tape.map((row) => row.p)).toEqual(["2", "1"]);
  });

  it("drops a republished tick", () => {
    // `ON CONFLICT DO NOTHING` drops duplicates on the write side, but
    // the writer publishes what it accepted, so the same (s, t) can
    // arrive twice.
    handleFrame({ op: Op.Tick, d: tick({ t: 1, p: "1" }) });
    paint();
    handleFrame({ op: Op.Tick, d: tick({ t: 1, p: "1" }) });
    paint();
    expect(useLive.getState().tape).toHaveLength(1);
  });

  it("holds only the symbol it is pointed at", () => {
    handleFrame({ op: Op.Tick, d: tick({ s: "MSFT", t: 1 }) });
    paint();
    expect(useLive.getState().tape).toEqual([]);
  });

  it("is bounded", () => {
    for (let index = 0; index < TAPE_MAX + 50; index += 1) {
      handleFrame({ op: Op.Tick, d: tick({ t: index }) });
    }
    paint();
    expect(useLive.getState().tape).toHaveLength(TAPE_MAX);
  });

  it("starts empty when it is pointed somewhere else", () => {
    // Two symbols' trades interleaved in one list is not a tape.
    handleFrame({ op: Op.Tick, d: tick({ t: 1 }) });
    paint();
    setTape("MSFT");
    expect(useLive.getState().tape).toEqual([]);
  });
});

describe("reference counting", () => {
  it("subscribes once and unsubscribes when the last panel goes", async () => {
    retain("AAPL");
    retain("AAPL");
    await Promise.resolve();
    release("AAPL");
    expect(sent()).toEqual([{ op: Op.Sub, symbols: ["AAPL"] }]);
    release("AAPL");
    expect(sent()).toEqual([
      { op: Op.Sub, symbols: ["AAPL"] },
      { op: Op.Unsub, symbols: ["AAPL"] },
    ]);
  });

  it("does not round-trip when one panel replaces another", async () => {
    // `GP -> QR` on the same symbol is a retain before a release, so it
    // never unsubscribes and asks for a second snapshot.
    retain("AAPL");
    await Promise.resolve();
    retain("AAPL");
    release("AAPL");
    expect(sent()).toEqual([{ op: Op.Sub, symbols: ["AAPL"] }]);
  });

  it("forgets the snapshot floor when the last panel goes", async () => {
    retain("AAPL");
    await Promise.resolve();
    handleFrame({ op: Op.Snap, d: tick({ t: 5_000 }) });
    release("AAPL");
    retain("AAPL");
    handleFrame({ op: Op.Tick, d: tick({ t: 10, p: "9" }) });
    paint();
    expect(useLive.getState().quotes.AAPL?.p).toBe("9");
  });
});

describe("status", () => {
  it("records whether ticks will flow at all", () => {
    handleFrame({ op: Op.Live, enabled: true });
    expect(useLive.getState().enabled).toBe(true);
  });

  it("accumulates what the server dropped", () => {
    handleFrame({ op: Op.Dropped, n: 3 });
    handleFrame({ op: Op.Dropped, n: 2 });
    expect(useLive.getState().dropped).toBe(5);
  });
});

describe("isRegularSession", () => {
  it("is true only for the regular session", () => {
    // `GIP` asks the API for `session=regular`; a live candle built from
    // extended-hours ticks would not match the bars under it.
    expect(isRegularSession(tick({ mh: MarketHours.Regular }))).toBe(true);
    expect(isRegularSession(tick({ mh: MarketHours.PreMarket }))).toBe(false);
    expect(isRegularSession(tick({ mh: MarketHours.PostMarket }))).toBe(false);
  });
});
