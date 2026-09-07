import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveSocket, RECONNECT_MAX_MS, RECONNECT_MIN_MS } from "./socket";
import type { SocketLike } from "./socket";
import { LinkState, Op } from "./types";

class FakeSocket implements SocketLike {
  sent: string[] = [];
  closed = false;
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
  }

  frames(): unknown[] {
    return this.sent.map((text) => JSON.parse(text) as unknown);
  }
}

let opened: FakeSocket[] = [];

function build(): { socket: LiveSocket; frames: unknown[]; states: LinkState[] } {
  const frames: unknown[] = [];
  const states: LinkState[] = [];
  const socket = new LiveSocket({
    onFrame: (frame) => frames.push(frame),
    onState: (state) => states.push(state),
    url: "ws://test/ui/ws",
    open: () => {
      const fake = new FakeSocket();
      opened.push(fake);
      return fake;
    },
  });
  return { socket, frames, states };
}

function latest(): FakeSocket {
  const socket = opened[opened.length - 1];
  if (socket === undefined) throw new Error("no socket was opened");
  return socket;
}

beforeEach(() => {
  opened = [];
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("subscriptions", () => {
  it("asks for a symbol once", () => {
    // A second `sub` answers with a second `snap`, and the strip would
    // flash the same price again on every panel switch.
    const { socket } = build();
    socket.start();
    latest().onopen?.({});
    socket.subscribe(["AAPL"]);
    socket.subscribe(["AAPL"]);
    expect(latest().frames()).toEqual([{ op: Op.Sub, symbols: ["AAPL"] }]);
  });

  it("only unsubscribes what it holds", () => {
    const { socket } = build();
    socket.start();
    latest().onopen?.({});
    socket.unsubscribe(["MSFT"]);
    expect(latest().frames()).toEqual([]);
  });

  it("resends the whole set on a reconnect", () => {
    // The server knows nothing after a reconnect, and "what is on screen
    // now" is the only correct answer.
    const { socket } = build();
    socket.start();
    latest().onopen?.({});
    socket.subscribe(["AAPL", "MSFT"]);
    latest().onclose?.({});
    vi.advanceTimersByTime(RECONNECT_MIN_MS);
    latest().onopen?.({});
    expect(latest().frames()).toEqual([{ op: Op.Sub, symbols: ["AAPL", "MSFT"] }]);
  });

  it("does not resend a symbol released while the socket was down", () => {
    const { socket } = build();
    socket.start();
    latest().onopen?.({});
    socket.subscribe(["AAPL", "MSFT"]);
    latest().onclose?.({});
    socket.unsubscribe(["MSFT"]);
    vi.advanceTimersByTime(RECONNECT_MIN_MS);
    latest().onopen?.({});
    expect(latest().frames()).toEqual([{ op: Op.Sub, symbols: ["AAPL"] }]);
  });
});

describe("reconnecting", () => {
  it("backs off exponentially and stops at the ceiling", () => {
    const { socket } = build();
    socket.start();
    for (const wait of [RECONNECT_MIN_MS, 2000, 4000, 8000, 16000, RECONNECT_MAX_MS]) {
      const before = opened.length;
      latest().onclose?.({});
      vi.advanceTimersByTime(wait - 1);
      expect(opened.length).toBe(before);
      vi.advanceTimersByTime(1);
      expect(opened.length).toBe(before + 1);
    }
    // The ceiling holds rather than doubling past it.
    const before = opened.length;
    latest().onclose?.({});
    vi.advanceTimersByTime(RECONNECT_MAX_MS);
    expect(opened.length).toBe(before + 1);
  });

  it("resets the backoff after a successful open", () => {
    const { socket } = build();
    socket.start();
    latest().onclose?.({});
    vi.advanceTimersByTime(RECONNECT_MIN_MS);
    latest().onopen?.({});
    const before = opened.length;
    latest().onclose?.({});
    vi.advanceTimersByTime(RECONNECT_MIN_MS);
    expect(opened.length).toBe(before + 1);
  });

  it("stops reconnecting once stopped", () => {
    const { socket } = build();
    socket.start();
    socket.stop();
    latest().onclose?.({});
    const before = opened.length;
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 2);
    expect(opened.length).toBe(before);
  });

  it("reports the link state as it changes", () => {
    const { socket, states } = build();
    socket.start();
    latest().onopen?.({});
    latest().onclose?.({});
    expect(states).toEqual([LinkState.Connecting, LinkState.Open, LinkState.Closed]);
  });

  it("retries when the constructor itself throws", () => {
    // A browser refuses `new WebSocket` outright on a mixed-content
    // page; without this the page would sit dead with no retry.
    const states: LinkState[] = [];
    let attempts = 0;
    const socket = new LiveSocket({
      onFrame: () => {},
      onState: (state) => states.push(state),
      url: "ws://test/ui/ws",
      open: () => {
        attempts += 1;
        throw new Error("refused");
      },
    });
    socket.start();
    vi.advanceTimersByTime(RECONNECT_MIN_MS);
    expect(attempts).toBe(2);
    socket.stop();
  });
});

describe("frames in", () => {
  it("hands over what the server sent", () => {
    const { socket, frames } = build();
    socket.start();
    latest().onmessage?.({ data: JSON.stringify({ op: Op.Live, enabled: true }) });
    expect(frames).toEqual([{ op: Op.Live, enabled: true }]);
  });

  it("ignores a frame that is not JSON", () => {
    // A proxy keepalive must not take the prices down.
    const { socket, frames } = build();
    socket.start();
    latest().onmessage?.({ data: "pong" });
    latest().onmessage?.({ data: new ArrayBuffer(4) });
    expect(frames).toEqual([]);
  });
});
