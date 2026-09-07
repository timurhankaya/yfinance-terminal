// The one scenario the charts have to pass before 1d is done: type a
// command, get candles.
//
// It asserts against the real canvas rather than the props a mock saw,
// which is the whole reason it exists -- `charts.test.tsx` proves the
// panel hands the right series over, and only a browser proves
// `lightweight-charts` then draws them. In particular it proves the page
// draws under `style-src 'self'`: the library styles its canvases
// through the CSSOM, and a `<style>` element would be blocked and the
// chart would come out unsized.
//
// `E2E_SYMBOL` picks the symbol; the default is one every archive of a
// US universe has. It has to be a symbol with intraday bars -- an
// archive that has never run `yfin bars sync --interval 5m` has nothing
// for this to draw, and the panel correctly says so.
import { expect, test } from "@playwright/test";

const SYMBOL = process.env.E2E_SYMBOL ?? "AAPL";

test("a typed command draws intraday candles", async ({ page }) => {
  const errors: string[] = [];
  // A CSP violation is reported here and nowhere else: the page still
  // renders, just without whatever was blocked.
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });

  // `/ui` is the home now; the command box is focused there too.
  await page.goto("/ui");
  const box = page.getByLabel("command");
  await expect(box).toBeFocused();

  await box.fill(`${SYMBOL} GIP 5m`);
  await box.press("Enter");

  // The URL is the state: every command is a history entry, so this is
  // also what makes Esc go back to where it was.
  await expect(page).toHaveURL(new RegExp(`/ui/t/${SYMBOL}/GIP\\?interval=5m$`));

  const chart = page.getByRole("img", { name: `${SYMBOL} 5m candles` });
  await expect(chart).toBeVisible();

  // A canvas with area is the difference between "the component
  // mounted" and "the library drew". An unsized container is what a
  // blocked stylesheet looks like.
  const canvas = chart.locator("canvas").first();
  await expect(canvas).toBeVisible();
  const box2 = await canvas.boundingBox();
  expect(box2?.width ?? 0).toBeGreaterThan(200);
  expect(box2?.height ?? 0).toBeGreaterThan(200);

  // The panel says what it drew, including whether the archive has
  // holes in this window -- silence there would read as "no gaps".
  await expect(page.getByText(/regular session/)).toBeVisible();
  await expect(page.getByText(/open gaps?|no open gaps/)).toBeVisible();

  expect(errors.filter((text) => text.includes("Content Security Policy"))).toEqual([]);
});

test("the daily chart marks corporate actions", async ({ page }) => {
  await page.goto(`/ui/t/${SYMBOL}/GP`);
  const chart = page.getByRole("img", { name: `${SYMBOL} daily candles` });
  await expect(chart).toBeVisible();
  await expect(page.getByText(/daily · 2 years/)).toBeVisible();
});
