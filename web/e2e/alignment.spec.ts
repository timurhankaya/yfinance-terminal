// Every heading sits over its own column.
//
// This is a browser test because it is a question about layout, and
// jsdom has none: a `<th>` and its `<td>` can carry the right classes
// and still be pushed apart by a rule that only applies to one of them,
// which is exactly the bug this was written after. The check is the same
// one a reader makes by eye -- where does the text in the heading start,
// and where does the text in the cell start -- so it is measured with a
// Range over the contents of each, and against the edge the alignment
// pushes them to.
import { expect, test } from "@playwright/test";

const SYMBOL = process.env.E2E_SYMBOL ?? "AAPL";

/** A page per table shape the terminal has: the shared engine, the
 *  screener's own columns, the watchlist's hand-written rows, the
 *  catalogue's typed grid, and the home page. */
const PAGES: ReadonlyArray<[name: string, path: string]> = [
  ["analyst sections", `/ui/t/${SYMBOL}/ANR`],
  ["a statement", `/ui/t/${SYMBOL}/FA`],
  ["price bars", `/ui/t/${SYMBOL}/PX`],
  ["a catalogue dataset", `/ui/m/DS?name=institutional_holders&symbol=${SYMBOL}`],
  ["a watchlist", `/ui/m/WLA?symbols=${SYMBOL}`],
  ["the home page", "/ui/"],
];

for (const [what, path] of PAGES) {
  test(`headings sit over their columns: ${what}`, async ({ page }) => {
    await page.goto(path, { waitUntil: "networkidle" });
    await expect(page.locator("table.grid tbody tr").first()).toBeVisible();

    const offset = await page.evaluate(() => {
      const norm = (value: string) => (value === "start" ? "left" : value === "end" ? "right" : value);
      const box = (element: Element) => {
        const range = document.createRange();
        range.selectNodeContents(element);
        return range.getBoundingClientRect();
      };
      const bad: string[] = [];
      for (const table of document.querySelectorAll("table.grid")) {
        const ths = [...table.querySelectorAll("thead th")];
        const first = table.querySelector("tbody tr");
        if (ths.length === 0 || first === null) continue;
        const cells = [...first.children];
        ths.forEach((th, index) => {
          const td = cells[index];
          if (td === undefined) return;
          const headAlign = norm(getComputedStyle(th).textAlign);
          const cellAlign = norm(getComputedStyle(td).textAlign);
          const head = box(th);
          const cell = box(td);
          const headEdge = headAlign === "right" ? th.getBoundingClientRect().right - head.right : head.left - th.getBoundingClientRect().left;
          const cellEdge = cellAlign === "right" ? td.getBoundingClientRect().right - cell.right : cell.left - td.getBoundingClientRect().left;
          if (headAlign !== cellAlign || Math.abs(headEdge - cellEdge) > 2) {
            bad.push(`${th.textContent?.trim() ?? ""}: heading ${headAlign} at ${String(Math.round(headEdge))}, cell ${cellAlign} at ${String(Math.round(cellEdge))}`);
          }
        });
      }
      return bad;
    });

    expect(offset).toEqual([]);
  });
}
