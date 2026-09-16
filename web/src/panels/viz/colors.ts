// Colour, read from the stylesheet: `styles.css` holds `--up`, `--down`
// and the seven group colours, and there is no second definition in
// TypeScript. Three roles that do not mix: DIRECTION (`--up`/`--down`),
// EMPHASIS (`--accent`), IDENTITY (the group colours). Read once and
// cached: a watchlist draws two hundred sparklines per paint.

/** The group letters a page can be split into, and the series colours a
 *  comparison uses. Seven, because an eighth is not distinguishable. */
export enum Group {
  A = "a",
  B = "b",
  C = "c",
  D = "d",
  E = "e",
  F = "f",
  G = "g",
}

export const GROUP_ORDER: readonly Group[] = [
  Group.A,
  Group.B,
  Group.C,
  Group.D,
  Group.E,
  Group.F,
  Group.G,
];

export interface VizTheme {
  bg: string;
  fg: string;
  muted: string;
  line: string;
  accent: string;
  up: string;
  down: string;
  group: Record<Group, string>;
}

//: The same values `styles.css` sets, for an environment with no
//: computed style to read -- jsdom, and a paint before the stylesheet
//: has arrived.
export const FALLBACK: VizTheme = {
  bg: "#080b10",
  fg: "#e2e8f0",
  muted: "#9aa9bc",
  line: "#2a3748",
  accent: "#ffc247",
  up: "#3ce5a0",
  down: "#ff6275",
  group: {
    [Group.A]: "#56b4e9",
    [Group.B]: "#e69f00",
    [Group.C]: "#009e73",
    [Group.D]: "#cc79a7",
    [Group.E]: "#f0e442",
    [Group.F]: "#0072b2",
    [Group.G]: "#d55e00",
  },
};

let cached: VizTheme | null = null;

/** The terminal's colours, read from the stylesheet once. */
export function vizTheme(): VizTheme {
  if (cached !== null) return cached;
  const style = getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string): string =>
    style.getPropertyValue(name).trim() || fallback;
  const group = {} as Record<Group, string>;
  for (const letter of GROUP_ORDER) {
    group[letter] = read(`--group-${letter}`, FALLBACK.group[letter]);
  }
  cached = {
    bg: read("--bg", FALLBACK.bg),
    fg: read("--fg", FALLBACK.fg),
    muted: read("--muted", FALLBACK.muted),
    line: read("--line", FALLBACK.line),
    accent: read("--accent", FALLBACK.accent),
    up: read("--up", FALLBACK.up),
    down: read("--down", FALLBACK.down),
    group,
  };
  return cached;
}

//: Everything that draws with these colours needs to know when they
//: change -- the reader can pick the accent, and a chart that kept the
//: old one would be the only thing on screen still wearing it.
const listeners = new Set<() => void>();

/** Watch for the palette changing. Returns the unsubscribe. */
export function subscribeVizTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Forget what was read, so the next call reads again, and tell everyone
 *  drawing with it. Called when the accent changes, and by tests. */
export function resetVizTheme(): void {
  cached = null;
  for (const listener of listeners) listener();
}

export function groupColor(group: Group): string {
  return vizTheme().group[group];
}

/** One colour per series, in the order the series were named. Throws
 *  past seven rather than wrapping: two series in the same colour is a
 *  chart that misleads, and the panel refuses the command instead. */
export function seriesColors(count: number): string[] {
  if (count > GROUP_ORDER.length) {
    throw new Error(`At most ${GROUP_ORDER.length} series can be told apart by colour`);
  }
  return GROUP_ORDER.slice(0, Math.max(0, count)).map(groupColor);
}

/** The colour of a change: up, down, or muted for no move at all. */
export function directionColor(change: number): string {
  const theme = vizTheme();
  if (!Number.isFinite(change) || change === 0) return theme.muted;
  return change > 0 ? theme.up : theme.down;
}

function channels(hex: string): [number, number, number] | null {
  const value = hex.trim();
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(value);
  if (short !== null) {
    return [1, 2, 3].map((i) => parseInt((short[i] ?? "0").repeat(2), 16)) as [number, number, number];
  }
  const long = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(value);
  if (long === null) return null;
  return [1, 2, 3].map((i) => parseInt(long[i] ?? "0", 16)) as [number, number, number];
}

function hex(channel: number): string {
  return Math.round(Math.min(255, Math.max(0, channel))).toString(16).padStart(2, "0");
}

/** `from` at t=0, `to` at t=1. Falls back to the nearer end when either
 *  colour is in a notation this cannot read. */
export function mix(from: string, to: string, t: number): string {
  const a = channels(from);
  const b = channels(to);
  if (a === null || b === null) return t < 0.5 ? from : to;
  return `#${a.map((channel, i) => hex(channel + ((b[i] ?? channel) - channel) * t)).join("")}`;
}

/** A heat colour for a percentage change: neutral at zero, `--up` at
 *  `+span`, `--down` at `-span`, clamped beyond. Diverging because zero
 *  is a meaningful value here, not one end of a range. The colour never
 *  carries the value on its own. */
export function divergingHeat(percent: number, span: number): string {
  const theme = vizTheme();
  if (!Number.isFinite(percent) || !Number.isFinite(span) || span <= 0) return theme.line;
  const t = Math.min(1, Math.abs(percent) / span);
  if (t === 0) return theme.line;
  return mix(theme.line, percent > 0 ? theme.up : theme.down, t);
}
