// The one colour the reader gets to choose.
//
// The accent is emphasis: focus rings, the active function, the command
// line, a chart's own line. It is deliberately not direction (green and
// red say that) and not identity (the seven group colours say that), so
// changing it cannot make a fall look like a rise or two groups look
// like one.
//
// It is a data attribute on the document rather than a class or an inline
// style: `styles.css` owns every value, and the page carries only which
// set is in force.
import { resetVizTheme } from "../panels/viz/colors";

export enum Accent {
  Amber = "amber",
  Blue = "blue",
  Violet = "violet",
}

export const ACCENTS: readonly Accent[] = [Accent.Amber, Accent.Blue, Accent.Violet];

export const ACCENT_KEY = "yfin.ui.accent";

/** The accent this browser was left on, or the amber the terminal has
 *  always opened with. */
export function readAccent(): Accent {
  try {
    const stored = localStorage.getItem(ACCENT_KEY);
    return ACCENTS.find((accent) => accent === stored) ?? Accent.Amber;
  } catch {
    // Storage the browser will not hand over: the default is still a
    // working terminal.
    return Accent.Amber;
  }
}

/** Puts an accent in force and remembers it.
 *
 *  The SVG primitives read colour from the stylesheet once and cache it
 *  -- two hundred sparklines cannot each ask the browser to recompute a
 *  style -- so the cache is dropped here. What is already drawn on a
 *  canvas redraws when its panel next renders. */
export function applyAccent(accent: Accent): void {
  document.documentElement.dataset.accent = accent;
  resetVizTheme();
  try {
    localStorage.setItem(ACCENT_KEY, accent);
  } catch {
    // A reader who cannot be remembered still gets the colour they asked
    // for, for as long as the tab is open.
  }
}
