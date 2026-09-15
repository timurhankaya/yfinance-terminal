import { expect, test } from "@playwright/test";

test("watchlist edits survive reload and all symbols can be removed", async ({ page }) => {
  await page.goto("/ui/m/WLA");
  await expect(page.getByRole("navigation", { name: "functions" }).getByRole("button", { name: "PG", exact: true })).toHaveCount(0);
  await page.getByLabel("Watchlist symbols").fill("aapl, MSFT aapl");
  await page.getByRole("button", { name: "Add symbols" }).click();
  await expect(page.getByRole("button", { name: "Remove AAPL" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove MSFT" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "Remove MSFT" })).toBeVisible();
  await page.getByRole("button", { name: "Remove MSFT" }).click();
  await page.getByRole("button", { name: "Remove AAPL" }).click();
  await expect(page.getByText("Nothing to watch yet.", { exact: false })).toBeVisible();
  await expect(page).toHaveURL(/\/ui\/m\/WLA$/);
});

test("Dockview is enabled by default and opens a workspace", async ({ page }) => {
  await page.goto("/ui/m/WLA?symbols=AAPL");
  await expect(page.locator('meta[name="yfin-dockview-enabled"]')).toHaveAttribute("content", "true");
  await page.getByRole("button", { name: "Open a second panel" }).click();
  await expect(page).toHaveURL(/\/ui\/w\/-$/);
  await expect(page.getByRole("table", { name: "watchlist", exact: true })).toHaveCount(2);
});

test("Pages is hidden in search and help", async ({ page }) => {
  await page.goto("/ui/m/HELP");
  await expect(page.getByRole("button", { name: "PG", exact: true })).toHaveCount(0);
  await expect(page.getByText("PG SAVE trading", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Search symbols and functions" }).click();
  await expect(page.getByRole("option", { name: /PG.*Saved pages/ })).toHaveCount(0);
});
