// The formatting primitives the tables and the panels share. Here rather
// than inside a panel: `table.tsx` is the generic typed table, and a
// dependency from it onto one concrete panel would couple the engine to
// that panel's existence.
import type { Row } from "../api/client";

/** A fixed locale: the terminal reads the same on a tr-TR machine as on
 *  an en-US one, and 36,61 next to 4.67T would be two number formats. */
export const LOCALE = "en-US";

/** A number scaled to K/M/B/T, two decimals. */
export function formatBig(value: number): string {
  const units: Array<[number, string]> = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [size, suffix] of units) {
    if (Math.abs(value) >= size) return `${(value / size).toFixed(2)}${suffix}`;
  }
  return value.toFixed(0);
}

/** A price or a price change: two decimals with thousands separators,
 *  never scaled. `2.49K` is not a price of ether; `2,487.12` is. */
export function formatPrice(value: unknown): string {
  const n = asNumber(value);
  if (n === null) return "—";
  return n.toLocaleString(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** A wire value as a finite number, or null. Decimals arrive as STRINGS
 *  (NUMERIC on the wire), so a string that parses counts. */
export function asNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

const HTTP_RE = /^https?:\/\//i;

/** True for a value that can be used as an `href`. */
export function isHttpUrl(value: unknown): value is string {
  return typeof value === "string" && HTTP_RE.test(value);
}

/** One field of a row as text, with a dash where there is nothing. */
export function text(row: Row, key: string): string {
  const value = row[key];
  return value === null || value === undefined ? "—" : String(value);
}

//: Past this many characters a value is prose rather than a fact, and a
//: field grid has to give it a row of its own -- squeezed into a 130
//: pixel half-cell, a business summary is a column of two-word lines
//: that pushes every field beside it down with it.
export const PROSE_CHARS = 120;

/** Whether a formatted value should take a whole row of a field grid. */
export function isProse(shown: unknown): boolean {
  return typeof shown === "string" && shown.length > PROSE_CHARS;
}
