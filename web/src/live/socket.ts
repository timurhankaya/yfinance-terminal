// One socket for the page, and the reconnect policy around it.
//
// The store above this decides WHAT is subscribed; this decides only that
// whatever is subscribed survives a dropped connection. The subscription
// set is resent on every open rather than replayed from a log: after a
// reconnect the server knows nothing, and "what is on screen now" is the
// only correct answer -- a replay would resubscribe panels the reader
// closed while the socket was down.
import { LinkState, Op, socketUrl } from "./types";
import type { ClientFrame, ServerFrame } from "./types";

/** The slice of WebSocket this uses, so a test can stand in for it. */
export interface SocketLike {
  send(data: string): void;
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
}

export interface LiveSocketOptions {
  onFrame(frame: ServerFrame): void;
  onState(state: LinkState): void;
  /** Seams for tests; the defaults are the browser's. */
  open?: (url: string) => SocketLike;
  url?: string;
}

//: The first retry is fast because the common cause is a deploy or a
//: laptop waking up, and the ceiling is 30 s because past that a reader
//: has reloaded the page anyway.
export const RECONNECT_MIN_MS = 1_000;
export const RECONNECT_MAX_MS = 30_000;

export class LiveSocket {
  private socket: SocketLike | null = null;
  private readonly symbols = new Set<string>();
  private ready = false;
  private backoff = RECONNECT_MIN_MS;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(private readonly options: LiveSocketOptions) {}

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.ready = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }

  /** Adds symbols and asks for them. Already-known ones are not re-sent:
   *  a second `sub` would answer with a second `snap`, and the strip
   *  would flash the same price twice on every panel switch. */
  subscribe(symbols: string[]): void {
    const fresh = symbols.filter((symbol) => !this.symbols.has(symbol));
    for (const symbol of fresh) this.symbols.add(symbol);
    if (fresh.length > 0) this.send({ op: Op.Sub, symbols: fresh });
  }

  /** Asks for the whole set again.
   *
   *  For one situation only: the server refuses a `sub` frame WHOLE when
   *  it would take the connection past its symbol ceiling
   *  (`ui/live.py`), so after such a refusal the client's idea of what is
   *  subscribed is ahead of the server's. `subscribe` cannot fix that --
   *  it deliberately skips symbols it has already asked for. */
  resubscribe(): void {
    if (this.symbols.size > 0) this.send({ op: Op.Sub, symbols: [...this.symbols] });
  }

  unsubscribe(symbols: string[]): void {
    const known = symbols.filter((symbol) => this.symbols.has(symbol));
    for (const symbol of known) this.symbols.delete(symbol);
    if (known.length > 0) this.send({ op: Op.Unsub, symbols: known });
  }

  private send(frame: ClientFrame): void {
    // Dropped rather than queued while the socket is not open: the set is
    // resent in full on the next open, so a queue would only duplicate
    // it. `ready` rather than trusting `send` to throw -- a browser
    // raises InvalidStateError before OPEN, but relying on that would
    // make the duplicate `sub` a browser-only bug.
    if (!this.ready) return;
    try {
      this.socket?.send(JSON.stringify(frame));
    } catch {
      // A socket that closed between the check and the send. The reopen
      // path resends everything.
    }
  }

  private connect(): void {
    if (this.stopped) return;
    this.ready = false;
    this.options.onState(LinkState.Connecting);
    const url = this.options.url ?? socketUrl(window.location);
    const open = this.options.open ?? ((target: string) => new WebSocket(target) as SocketLike);
    let socket: SocketLike;
    try {
      socket = open(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.ready = true;
      this.backoff = RECONNECT_MIN_MS;
      this.options.onState(LinkState.Open);
      if (this.symbols.size > 0) {
        this.send({ op: Op.Sub, symbols: [...this.symbols] });
      }
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      let frame: ServerFrame;
      try {
        frame = JSON.parse(event.data) as ServerFrame;
      } catch {
        return;
      }
      this.options.onFrame(frame);
    };
    socket.onclose = () => {
      this.ready = false;
      this.socket = null;
      this.options.onState(LinkState.Closed);
      this.scheduleReconnect();
    };
    // A socket that errors also closes, so `onclose` owns the reconnect
    // and this only stops the error reaching the console as unhandled.
    socket.onerror = () => {};
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.timer !== null) return;
    const wait = this.backoff;
    this.backoff = Math.min(this.backoff * 2, RECONNECT_MAX_MS);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, wait);
  }
}
