// The one colour the reader gets to choose. The accent is emphasis only:
// not direction (green and red) and not identity (the seven group
// colours), so changing it cannot make a fall look like a rise. It is a
// data attribute on the document: `styles.css` owns every value.
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

/** Puts an accent in force and remembers it. The SVG primitives cache
 *  the stylesheet's colours, so the cache is dropped here; a canvas
 *  redraws when its panel next renders. */
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
