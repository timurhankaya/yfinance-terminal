// Colour, read from the stylesheet.
//
// There is no second definition of a colour in TypeScript. `styles.css`
// holds `--up`, `--down` and the seven group colours, and this module
// reads them the way `Chart.tsx` already reads `--bg` and `--accent`
// (`Chart.tsx:36-49`): one source, so a change to the palette moves the
// tables, the chart and every SVG here together.
//
// Three roles, and they do not mix. DIRECTION is `--up`/`--down` -- a
// rise and a fall. EMPHASIS is `--accent` -- focus and selection.
// IDENTITY is the seven group colours -- which series, which group, and
// nothing about whether it went up.
//
// Read once, unlike `Chart.tsx`. That file builds one chart; a watchlist
// draws two hundred sparklines, and `getComputedStyle` two hundred times
// per paint is a cost with nothing to show for it. The stylesheet does
// not change at runtime, and `resetVizTheme` exists for the tests that
// need it to.

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
  bg: "#0b0e11",
  fg: "#d7dde3",
  muted: "#7f8a96",
  line: "#1f262e",
  accent: "#f2b544",
  up: "#4cc38a",
  down: "#ff6b6b",
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

/** Tests only: forget what was read, so the next call reads again. */
export function resetVizTheme(): void {
  cached = null;
}

export function groupColor(group: Group): string {
  return vizTheme().group[group];
}

/** One colour per series, in the order the series were named.
 *
 *  Throws past seven rather than wrapping: two series in the same colour
 *  is a chart that misleads, and the panel refuses the command instead
 *  (spec, "Hata yönetimi"). */
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
 *  `+span`, `--down` at `-span`, clamped beyond.
 *
 *  Diverging and neutral in the middle, because zero is a meaningful
 *  value here rather than one end of a range -- a sequential ramp would
 *  make "unchanged" look like the low end of "fell". The colour never
 *  carries the value on its own: the number is in the cell where there
 *  is room for it, and in the row detail regardless (spec, "Kararlar" 7). */
export function divergingHeat(percent: number, span: number): string {
  const theme = vizTheme();
  if (!Number.isFinite(percent) || !Number.isFinite(span) || span <= 0) return theme.line;
  const t = Math.min(1, Math.abs(percent) / span);
  if (t === 0) return theme.line;
  return mix(theme.line, percent > 0 ? theme.up : theme.down, t);
}
