import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WireType, type CatalogColumn } from "../api/client";
import { DatasetTable, formatCell, formatDateTime, formatDecimal, formatInteger, rawDecimal } from "./table";

afterEach(cleanup);

const COLUMNS: CatalogColumn[] = [
  { name: "symbol", type: WireType.String, nullable: false },
  { name: "as_of_date", type: WireType.Date, nullable: false },
  { name: "value", type: WireType.Decimal, nullable: true },
  { name: "count", type: WireType.Integer, nullable: true },
  { name: "flag", type: WireType.Boolean, nullable: true },
  { name: "url", type: WireType.String, nullable: true },
  { name: "raw_json", type: WireType.String, nullable: true },
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
    expect(formatDecimal("0.664350000000")).toBe("0.6643"); // binary float: 0.66435 sits just under
    expect(formatDecimal("0.016480000000")).toBe("0.0165");
    expect(formatDecimal("7.49")).toBe("7.49");
    expect(rawDecimal("0.664350000000")).toBe("0.66435");
    expect(rawDecimal("2.000000000000")).toBe("2");
    expect(rawDecimal("12")).toBe("12");
    expect(formatDecimal("1234567")).toBe("1.23M");
    expect(formatDecimal(null)).toBe("—");
    expect(formatInteger(7751)).toBe("7,751");
    expect(formatCell(true, WireType.Boolean)).toBe("yes");
    expect(formatCell(false, WireType.Boolean)).toBe("no");
    expect(formatCell(null, WireType.String)).toBe("—");
    expect(formatCell("2026-09-06", WireType.Date)).toBe("2026-09-06");
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
    expect(detail.textContent).toContain("0.66435"); // full precision, not the grid's rounding
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

  it("opens the detail in a right-hand pane and offers Load more when asked", () => {
    const more = vi.fn();
    const { container } = render(<DatasetTable columns={COLUMNS} rows={ROWS} onLoadMore={more} />);
    expect(container.querySelector(".dataset.split")).toBeNull();
    expect(screen.getByText(/2 rows loaded, more available/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(more).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getAllByRole("row")[1]!);
    const split = container.querySelector(".dataset.split")!;
    expect(split).not.toBeNull();
    const detail = screen.getByRole("region", { name: "row detail" });
    expect(detail.closest(".split-detail")).not.toBeNull();
    expect(split.querySelector(".split-list table")).not.toBeNull();
  });

  it("shows Loading while the next page is in flight", () => {
    render(<DatasetTable columns={COLUMNS} rows={ROWS} onLoadMore={() => undefined} loadingMore />);
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();
  });

  it("says when the list was cut at the page cap", () => {
    render(<DatasetTable columns={COLUMNS} rows={ROWS} truncated />);
    expect(screen.getByText(/the list was cut/)).toBeTruthy();
  });
});
