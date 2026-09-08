// The page's one live store, and the three rules that keep it cheap.
//
// **Coalescing.** A busy symbol ticks faster than the screen refreshes.
// Frames land in a buffer and one `requestAnimationFrame` applies the
// whole batch, so React renders once per frame however many arrived --
// without this, a market open is a render storm that makes the terminal
// slower the more data it has.
//
// **Reference counting.** Panels retain a symbol and release it; the
// socket unsubscribes when the count reaches zero. `GP -> QR` on the same
// symbol is a retain before a release, so it never round-trips through
// an unsubscribe and back, and never asks for a second snapshot.
//
// **One tape.** Every symbol keeps its last tick, but only the symbol
// `QR` is showing keeps a history, and that history is a bounded ring.
// Keeping every symbol's would grow without limit for a panel nobody has
// open.
import { create } from "zustand";
import { LinkState, MarketHours, Op, WsErrorCode } from "./types";
import type { ServerFrame, Tick } from "./types";
import { LiveSocket } from "./socket";
import type { SocketLike } from "./socket";

//: What `QR` can scroll back through. A tick is ~150 bytes here, so this
//: is a third of a megabyte for the one symbol on screen.
export const TAPE_MAX = 2000;

export interface LiveState {
  /** False when the stream is not publishing or the API cannot reach the
   *  bus. The strip says so rather than showing a price that will never
   *  move. */
  enabled: boolean;
  link: LinkState;
  /** Ticks the server dropped for this connection, cumulative. */
  dropped: number;
  /** True once the server has refused a subscription for being over the
   *  connection's symbol ceiling. A page can reach it without any one
   *  panel being unreasonable: two watchlists are 400 symbols. */
  budgetFull: boolean;
  quotes: Record<string, Tick>;
  tapeSymbol: string | null;
  tape: Tick[];
}

export const useLive = create<LiveState>(() => ({
  enabled: false,
  link: LinkState.Closed,
  dropped: 0,
  budgetFull: false,
  quotes: {},
  tapeSymbol: null,
  tape: [],
}));

// --- ingest ----------------------------------------------------------------

const pending = new Map<string, Tick[]>();
let frameHandle: number | null = null;
//: The snapshot's timestamp per symbol. A tick older than it is a
//: message that was already in flight when the snapshot was read.
const snapAt = new Map<string, number>();
//: `(symbol, t)` already on the tape. The writer republishes rows that
//: `ON CONFLICT DO NOTHING` dropped, so the same tick can arrive twice.
let tapeSeen = new Set<string>();

function key(tick: Tick): string {
  return `${tick.s}|${tick.t}`;
}

function flush(): void {
  frameHandle = null;
  if (pending.size === 0) return;
  const batch = [...pending.entries()];
  pending.clear();
  useLive.setState((state) => {
    const quotes = { ...state.quotes };
    let tape = state.tape;
    for (const [symbol, ticks] of batch) {
      const last = ticks[ticks.length - 1];
      if (last !== undefined) quotes[symbol] = last;
      if (symbol !== state.tapeSymbol) continue;
      const fresh = ticks.filter((tick) => !tapeSeen.has(key(tick)));
      for (const tick of fresh) tapeSeen.add(key(tick));
      // Newest first: `QR` reads top-down like a time-and-sales window.
      tape = [...fresh.reverse(), ...tape].slice(0, TAPE_MAX);
    }
    if (tape !== state.tape && tapeSeen.size > TAPE_MAX * 2) {
      // The set only exists to catch republished rows, which arrive
      // within a batch or two; rebuilding it from the ring keeps it from
      // growing for the life of the page.
      tapeSeen = new Set(tape.map(key));
    }
    return { quotes, tape };
  });
}

function schedule(): void {
  if (frameHandle !== null) return;
  frameHandle = requestAnimationFrame(flush);
}

function accept(tick: Tick): void {
  const floor = snapAt.get(tick.s);
  if (floor !== undefined && tick.t < floor) return;
  const queued = pending.get(tick.s);
  if (queued === undefined) pending.set(tick.s, [tick]);
  else queued.push(tick);
  schedule();
}

export function handleFrame(frame: ServerFrame): void {
  switch (frame.op) {
    case Op.Live:
      useLive.setState({ enabled: frame.enabled });
      return;
    case Op.Snap:
      snapAt.set(frame.d.s, frame.d.t);
      accept(frame.d);
      return;
    case Op.Tick:
      accept(frame.d);
      return;
    case Op.Dropped:
      useLive.setState((state) => ({ dropped: state.dropped + frame.n }));
      return;
    case Op.Error:
      // `too_many` is recorded, because the server refuses the frame
      // WHOLE: every symbol in it stays unsubscribed and the panels that
      // asked would otherwise sit there quietly showing no price, which
      // looks exactly like a quiet market. `bad_symbol` is still
      // ignored: it names one token the page should not have sent, and
      // the panel already shows what it has.
      if (frame.code === WsErrorCode.TooMany) useLive.setState({ budgetFull: true });
      return;
  }
}

// --- the socket ------------------------------------------------------------

let socket: LiveSocket | null = null;
let openOverride: ((url: string) => SocketLike) | null = null;
const retained = new Map<string, number>();

/** Tests replace the transport; nothing else calls this. */
export function setSocketFactory(open: ((url: string) => SocketLike) | null): void {
  openOverride = open;
}

function live(): LiveSocket {
  if (socket === null) {
    socket = new LiveSocket({
      onFrame: handleFrame,
      onState: (link) => useLive.setState({ link }),
      ...(openOverride === null ? {} : { open: openOverride, url: "ws://test/ui/ws" }),
    });
    socket.start();
  }
  return socket;
}

export function retain(symbol: string): void {
  const count = retained.get(symbol) ?? 0;
  retained.set(symbol, count + 1);
  if (count === 0) live().subscribe([symbol]);
}

export function release(symbol: string): void {
  const count = retained.get(symbol) ?? 0;
  if (count <= 1) {
    retained.delete(symbol);
    snapAt.delete(symbol);
    socket?.unsubscribe([symbol]);
    // Room again. Closing a panel is the reader's own answer to a full
    // budget, so the set is asked for once more rather than waiting for
    // a reconnection to do it. The duplicate `snap` this can cause for
    // symbols the server did accept is the price of the retry, and it is
    // paid only after a refusal.
    if (useLive.getState().budgetFull) {
      useLive.setState({ budgetFull: false });
      socket?.resubscribe();
    }
    return;
  }
  retained.set(symbol, count - 1);
}

/** Points the tape at one symbol, or nowhere. The ring is dropped on the
 *  way: it is `QR`'s scrollback for the symbol it was showing, and
 *  carrying it into another symbol would interleave two tapes. */
export function setTape(symbol: string | null): void {
  tapeSeen = new Set();
  useLive.setState({ tapeSymbol: symbol, tape: [] });
}

/** Tests only: back to a page that has just loaded. */
export function resetLive(): void {
  socket?.stop();
  socket = null;
  retained.clear();
  snapAt.clear();
  pending.clear();
  tapeSeen = new Set();
  if (frameHandle !== null) cancelAnimationFrame(frameHandle);
  frameHandle = null;
  useLive.setState({
    enabled: false,
    link: LinkState.Closed,
    dropped: 0,
    budgetFull: false,
    quotes: {},
    tapeSymbol: null,
    tape: [],
  });
}

// --- reading ---------------------------------------------------------------

/** True when the tick belongs to the regular session.
 *
 *  `session=regular` is what `GIP` asks the API for, so a live candle
 *  built from extended-hours ticks would not match the bars under it. */
export function isRegularSession(tick: Tick): boolean {
  return tick.mh === MarketHours.Regular;
}
