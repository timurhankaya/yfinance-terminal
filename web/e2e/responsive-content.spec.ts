import { expect, test } from "@playwright/test";

for (const width of [390, 768, 1720, 3440]) {
  test(`financial chart fills its panel without growing too tall at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/ui/t/AKBNK.IS/FA");
    const chart = page.getByRole("img", { name: /revenue and net income/ });
    await expect(chart).toBeVisible({ timeout: 15000 });
    await expect.poll(async () => Number((await chart.getAttribute("viewBox"))?.split(" ")[2])).toBeGreaterThan(width - 80);
    const bounds = await chart.boundingBox();
    expect(bounds!.height).toBeLessThanOrEqual(401);
    expect(bounds!.width).toBeGreaterThan(width - 80);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    const dates = (await page.getByRole("columnheader").allTextContents()).filter((text) => /^\d{4}-/.test(text));
    expect(dates.length).toBeGreaterThan(1);
    expect(dates).toEqual([...dates].sort());
  });

  test(`holders keeps context when empty and fills the table width at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/ui/t/AKBNK.IS/HDS?tab=roster");
    await expect(page.getByRole("tab", { name: "Insider roster", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "No records available" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
    await expect(page.getByRole("table")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.getByRole("tab", { name: "Major", exact: true }).click();
    const table = page.getByRole("table");
    await expect(table).toBeVisible();
    const box = await table.boundingBox();
    expect(box!.width).toBeGreaterThan(width - 80);
    const footer = page.getByLabel("Table pagination");
    await expect(footer).toHaveCSS("border-top-width", "1px");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}
