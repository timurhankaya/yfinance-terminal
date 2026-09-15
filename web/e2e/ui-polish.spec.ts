import { expect, test } from "@playwright/test";

// Opt in when testing a production preview: Vite development injects styles.
if (process.env.E2E_ENFORCE_CSP === "1") {
  test.beforeEach(async ({ page }) => {
    await page.route("**/*", async (route) => {
      if (route.request().resourceType() !== "document") return route.continue();
      const response = await route.fetch();
      await route.fulfill({ response, headers: { ...response.headers(),
        "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data: https:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
      } });
    });
    await page.addInitScript(() => {
      document.addEventListener("securitypolicyviolation", () => { document.documentElement.dataset.cspViolation = "true"; });
    });
  });
  test.afterEach(async ({ page }) => {
    await expect(page.locator("html")).not.toHaveAttribute("data-csp-violation", "true");
  });
}

for (const width of [390, 1440]) {
  test(`accent controls remain usable at ${width}px and persist`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/ui/");
    const picker = page.getByRole("group", { name: "accent colour" });
    for (const colour of ["amber", "blue", "violet"]) {
      const button = picker.getByRole("button", { name: colour, exact: true });
      const box = await button.boundingBox();
      expect(box?.width).toBeGreaterThanOrEqual(24);
      expect(box?.height).toBeGreaterThanOrEqual(24);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      await button.click();
      await expect(page.locator("html")).toHaveAttribute("data-accent", colour);
    }
    await page.reload();
    await expect(picker.getByRole("button", { name: "violet", exact: true })).toHaveAttribute("aria-pressed", "true");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}

test("function help is visible on hover and focus and dismissible with Escape", async ({ page }) => {
  await page.goto("/ui/");
  const tab = page.getByRole("navigation", { name: "functions", exact: true }).getByRole("button", { name: "EQS", exact: true });
  await tab.hover();
  await expect(page.getByRole("tooltip")).toContainText("screen");
  await expect(tab).toHaveAttribute("aria-describedby", /.+/);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await page.mouse.move(0, 0);
  await tab.focus();
  await expect(page.getByRole("tooltip")).toBeVisible();
  await page.getByRole("textbox", { name: "command", exact: true }).focus();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
});

test("wide financial tables scroll without moving the whole narrow panel", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/t/AAPL/FA");
  await expect(page.locator("table.grid tbody tr").first()).toBeVisible();
  const sizes = await page.locator(".dock-panel").evaluate((panel) => ({ width: panel.clientWidth, scroll: panel.scrollWidth }));
  expect(sizes.scroll).toBeLessThanOrEqual(sizes.width + 1);
  const scroll = page.locator(".table-scroll").first();
  await expect(scroll).toBeVisible();
  expect(await scroll.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
});

test("mover prices and changes share column edges", async ({ page }) => {
  await page.goto("/ui/");
  const movers = page.locator(".home-movers");
  await movers.scrollIntoViewIfNeeded();
  await expect(movers.locator(".list-row").first()).toBeVisible();
  const edges = await movers.locator(".list").first().locator(".list-row").evaluateAll((rows) => rows.slice(0, 5).map((row) => row.querySelector(".num")!.getBoundingClientRect().right));
  expect(edges.length).toBeGreaterThan(1);
  expect(Math.max(...edges) - Math.min(...edges)).toBeLessThan(1);
});

test("sector labels remain readable inside a home tile", async ({ page }) => {
  await page.goto("/ui/");
  const label = page.locator(".viz-treemap .viz-cell-label").first();
  await expect(label).toBeVisible();
  const size = await label.evaluate((el) => Number.parseFloat(getComputedStyle(el).fontSize) * (el as SVGGraphicsElement).getScreenCTM()!.a);
  expect(size).toBeGreaterThanOrEqual(10);
});


test("numeric headings keep their text aligned after sorting", async ({ page }) => {
  await page.goto("/ui/t/AAPL/FA");
  const header = page.locator("th.num").first();
  await expect(header).toBeVisible();
  await header.getByRole("button").click();
  await expect(header).toHaveAttribute("aria-sort", "ascending");
  const offsets = await header.evaluate((th) => {
    const cell = th.closest("table")!.querySelector("tbody tr")!.children[(th as HTMLTableCellElement).cellIndex]!;
    const headText = document.createRange();
    headText.selectNodeContents(th.querySelector("button")!.firstChild!);
    const cellText = document.createRange();
    cellText.selectNodeContents(cell);
    return [th.getBoundingClientRect().right - headText.getBoundingClientRect().right, cell.getBoundingClientRect().right - cellText.getBoundingClientRect().right];
  });
  expect(Math.abs(offsets[0]! - offsets[1]!)).toBeLessThan(1);
});

for (const width of [390, 1440]) {
  test(`search opens a symbol detail without overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/ui/");
    const trigger = page.getByRole("button", { name: "Search symbols and functions" });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "Search the terminal" });
    await expect(dialog).toBeVisible();
    const input = dialog.getByRole("combobox", { name: "palette" });
    await expect(input).toBeFocused();
    await input.fill("AAPL");
    await dialog.getByRole("option", { name: /^AAPL —/ }).first().click();
    await expect(page.getByRole("heading", { name: "Apple Inc.", exact: true })).toBeVisible();
    await expect(dialog).toHaveCount(0);
    const sizes = await page.locator(".dock-panel").evaluate((panel) => ({ width: panel.clientWidth, scroll: panel.scrollWidth }));
    expect(sizes.scroll).toBeLessThanOrEqual(sizes.width + 1);
    await page.getByRole("navigation", { name: "symbol quick links" }).getByRole("button", { name: /Financials/ }).click();
    await expect(page.getByRole("tab", { name: "Income", exact: true })).toBeVisible();
  });
}

test("search traps keyboard focus and restores it on Escape", async ({ page }) => {
  await page.goto("/ui/");
  const trigger = page.getByRole("button", { name: "Search symbols and functions" });
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close search" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("combobox")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("symbol search resolves PG as a security and supports single-letter tickers", async ({ page }) => {
  await page.goto("/ui/t/AKBNK.IS/DES");
  const search = page.getByRole("button", { name: "Search symbols and functions" });
  await search.click();
  await page.getByRole("combobox", { name: "palette" }).fill("PG");
  await page.getByRole("option", { name: /^PG — The Procter/ }).click();
  await expect(page).toHaveURL(/\/ui\/t\/PG\/DES$/);
  await expect(page.getByRole("heading", { name: /Procter/ })).toBeVisible();
  await search.click();
  await page.getByRole("combobox", { name: "palette" }).fill("F");
  await page.getByRole("option", { name: /^F — Ford/ }).click();
  await expect(page).toHaveURL(/\/ui\/t\/F\/DES$/);
});

for (const width of [390, 1440]) {
  test(`dataset filters recover from empty results and paginate at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/ui/m/DS?name=screen_members&rows=2&screen_key=tr_equity");
    const tableRows = page.locator("table.grid tbody tr");
    await expect(tableRows).toHaveCount(2);
    const first = await tableRows.first().innerText();
    const more = page.getByRole("button", { name: "Load more", exact: true });
    await more.click();
    await expect(tableRows).toHaveCount(4);
    expect(await tableRows.first().innerText()).toBe(first);
    const size = page.getByRole("spinbutton", { name: /Rows per load/ });
    await expect(size).toHaveValue("2");
    const local = page.getByRole("searchbox", { name: "filter the rows already loaded" });
    await local.fill("zzz-no-symbol");
    await expect(page.getByText(/No loaded rows match/)).toBeVisible();
    await expect(more).toBeVisible();
    await page.getByRole("button", { name: "Clear row filter" }).click();
    await expect(tableRows).toHaveCount(4);
    await expect(page.locator(".dataset-filters")).not.toHaveAttribute("open");
    await page.locator(".dataset-filters summary").click();
    const server = page.getByRole("textbox", { name: "screen_key", exact: true });
    await server.fill("no-such-screen");
    await page.getByRole("button", { name: "Apply filters" }).click();
    await expect(page.getByText(/No records for these filters/)).toBeVisible();
    await page.locator(".dataset-filters summary").click();
    await expect(server).toBeVisible();
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(tableRows).toHaveCount(2);
    expect(new URL(page.url()).searchParams.has("screen_key")).toBe(false);
    const sizes = await page.locator(".dock-panel").evaluate((el) => [el.clientWidth, el.scrollWidth]);
    expect(sizes[1]).toBeLessThanOrEqual(sizes[0]! + 1);
    const footer = await page.locator(".table-footer").boundingBox();
    expect(footer!.x + footer!.width).toBeLessThanOrEqual(width);
  });
}

test("EQS appends members and keeps its filter and pagination usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.goto("/ui/m/EQS?screen=tr_equity");
  const tableRows = page.locator("table.grid tbody tr");
  await expect(tableRows).toHaveCount(250);
  const size = page.getByRole("spinbutton", { name: /Rows per load/ });
  await size.fill("2");
  await size.press("Enter");
  await expect(page).toHaveURL(/rows=2/);
  await page.reload();
  await expect(size).toHaveValue("2");
  await expect(tableRows).toHaveCount(2);
  const first = await tableRows.first().innerText();
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(tableRows).toHaveCount(4);
  expect(await tableRows.first().innerText()).toBe(first);
  const filter = page.getByRole("searchbox", { name: "filter the rows already loaded" });
  await filter.fill("zz-no-match");
  await expect(page.getByText(/No loaded symbols match/)).toBeVisible();
  await page.getByRole("button", { name: "Clear row filter" }).click();
  await expect(tableRows).toHaveCount(4);
});

for (const width of [390, 1440]) {
  test(`symbol URLs and tabs stay in a single page until plus at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/ui/t/akbnk.is");
    await expect(page).toHaveURL(/\/ui\/t\/AKBNK.IS\/DES$/);
    await expect(page.locator(".single-panel")).toHaveCount(1);
    await expect(page.locator(".dock")).toHaveCount(0);
    const nav = page.getByRole("navigation", { name: "AKBNK.IS functions" });
    await nav.getByRole("button", { name: "HDS", exact: true }).click();
    await expect(page).toHaveURL(/\/ui\/t\/AKBNK.IS\/HDS$/);
    await expect(page.locator(".dock")).toHaveCount(0);
    await page.getByRole("tab", { name: "Institutions", exact: true }).click();
    await expect(page).toHaveURL(/tab=inst/);
    const tabUrl = page.url();
    await page.reload();
    await expect(page.getByRole("tab", { name: "Institutions", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.goBack();
    await expect(page.getByRole("tab", { name: "Major", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.goForward();
    await expect(page).toHaveURL(tabUrl);
    const input = page.getByRole("textbox", { name: "command", exact: true });
    await input.fill("AKBNK.IS DES");
    await input.press("Control+Enter");
    await expect(page).toHaveURL(/\/ui\/t\/AKBNK.IS\/DES$/);
    await expect(page.locator(".dock")).toHaveCount(0);
    await page.getByRole("button", { name: "Open a second panel", exact: true }).click();
    await expect(page).toHaveURL(/\/ui\/w\/-$/);
    await expect(page.locator(".dock-panel")).toHaveCount(2);
    await expect(page.locator(".dock")).toHaveCount(1);
    await page.goBack();
    await expect(page).toHaveURL(/\/ui\/t\/AKBNK.IS\/DES$/);
    await expect(page.locator(".single-panel")).toHaveCount(1);
    await expect(page.locator(".dock")).toHaveCount(0);
  });
}
