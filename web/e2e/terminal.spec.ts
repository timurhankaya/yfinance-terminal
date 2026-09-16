// Type a command, get candles. Asserted against the real canvas: only a
// browser proves `lightweight-charts` draws under `style-src 'self'` (it
// styles its canvases through the CSSOM; a `<style>` element would be
// blocked and the chart unsized). `E2E_SYMBOL` picks the symbol; it needs
// intraday bars (`yfin bars sync --interval 5m`) or the panel says so.
import { expect, test } from "@playwright/test";

const SYMBOL = process.env.E2E_SYMBOL ?? "AAPL";

// An archived fixture may have no bars in the real current nine-day window.
// Pin only the browser clock, explicitly, without changing the API or data.
test.beforeEach(async ({ page }) => {
  if (process.env.E2E_NOW) await page.clock.setFixedTime(new Date(process.env.E2E_NOW));
});

test("a typed command draws intraday candles", async ({ page }) => {
  const errors: string[] = [];
  // A CSP violation is reported here and nowhere else: the page still
  // renders, just without whatever was blocked.
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });

  // `/ui` is the home now; the command box is focused there too.
  await page.goto("/ui/");
  const box = page.getByLabel("command");
  await expect(box).toBeFocused();

  // `GP 5m`, not `GIP 5m`: one chart function takes every interval now.
  await box.fill(`${SYMBOL} GP 5m`);
  await box.press("Enter");

  // The URL is the state: every command is a history entry, so this is
  // also what makes Esc go back to where it was.
  await expect(page).toHaveURL(
    new RegExp(`/ui/t/${SYMBOL}/GP\\?interval=5m&years=2$`),
  );

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
  const chart = page.getByRole("img", { name: `${SYMBOL} 1d candles` });
  await expect(chart).toBeVisible();
  await expect(page.getByText(/1d · 2 years/)).toBeVisible();
});
