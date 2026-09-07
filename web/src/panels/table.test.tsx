import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { CatalogColumn } from "../api/client";
import { DatasetTable, formatCell, formatDateTime, formatDecimal, formatInteger } from "./table";

afterEach(cleanup);

const COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: "string", nullable: false },
  { name: "as_of_date", type: "string (date)", nullable: false },
  { name: "value", type: "string (decimal)", nullable: true },
  { name: "count", type: "integer", nullable: true },
  { name: "flag", type: "boolean", nullable: true },
  { name: "url", type: "string", nullable: true },
  { name: "raw_json", type: "string", nullable: true },
];

const ROWS = [
  {
    symbol: "AAPL",
    as_of_date: "2026-09-06",
    value: "0.664350000000",
    count: 7751,
    flag: true,
    url: "https://example.com/a",
    raw_json: '{"a":1}',
    unlisted: "kept",
  },
  { symbol: "AAPL", as_of_date: "2026-09-05", value: "1234567", count: null, flag: false, url: null, raw_json: null },
];

describe("cell formatting by wire type", () => {
  it("formats decimals, integers, booleans, dates and nulls", () => {
    expect(formatDecimal("0.664350000000")).toBe("0.66");
    expect(formatDecimal("1234567")).toBe("1.23M");
    expect(formatDecimal(null)).toBe("—");
    expect(formatInteger(7751)).toBe("7,751");
    expect(formatCell(true, "boolean")).toBe("yes");
    expect(formatCell(false, "boolean")).toBe("no");
    expect(formatCell(null, "string")).toBe("—");
    expect(formatCell("2026-09-06", "string (date)")).toBe("2026-09-06");
    expect(formatDateTime("not a date")).toBe("not a date");
    expect(formatDateTime("2026-09-06T11:44:28.632399Z")).toBe("2026-09-06 11:44 UTC");
    expect(formatDateTime("2026-09-06T00:00:00Z")).toBe("2026-09-06");
    expect(formatInteger(1974, "year_born")).toBe("1974");
    expect(formatInteger(2025, "fiscal_year")).toBe("2025");
  });
});

describe("DatasetTable", () => {
  it("hides raw_json and hidden columns from the grid but keeps them in the row detail", () => {
    render(<DatasetTable columns={COLUMNS} rows={ROWS} hide={["symbol"]} />);
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(["as_of_date", "value", "count", "flag", "url"]);
    expect(screen.getByText(/2 rows/)).toBeTruthy();
    expect(screen.getByText(/5 of 7 columns/)).toBeTruthy();

    fireEvent.click(screen.getAllByRole("row")[1]!);
    const detail = screen.getByRole("region", { name: "row detail" });
    expect(detail.textContent).toContain("symbol");
    expect(detail.textContent).toContain("AAPL");
    expect(detail.textContent).toContain('"a": 1'); // raw_json pretty-printed
    expect(detail.textContent).toContain("unlisted"); // a field the catalogue did not list
    expect(detail.textContent).toContain("kept");
  });

  it("renders URL strings as links and numbers right-aligned", () => {
    render(<DatasetTable columns={COLUMNS} rows={ROWS} />);
    const link = screen.getByRole("link", { name: "https://example.com/a" });
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    const numeric = screen.getAllByRole("cell").filter((td) => td.className === "num");
    expect(numeric.length).toBe(4); // value and count, two rows
  });

  it("opens the selected row on Enter and reverses when asked", () => {
    render(<DatasetTable columns={COLUMNS} rows={ROWS} reverse />);
    const first = screen.getAllByRole("row")[1]!;
    expect(first.textContent).toContain("2026-09-05");
    fireEvent.keyDown(window, { key: "Enter" });
    expect(screen.getByRole("region", { name: "row detail" }).textContent).toContain("2026-09-05");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("region", { name: "row detail" })).toBeNull();
  });

  it("says when the list was cut at the page cap", () => {
    render(<DatasetTable columns={COLUMNS} rows={ROWS} truncated />);
    expect(screen.getByText(/the list was cut/)).toBeTruthy();
  });
});
