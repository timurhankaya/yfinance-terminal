import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { SortedTable, type Column } from "./common";

interface Row extends Record<string, unknown> {
  symbol: string;
  price: string | null;
  as_of: string;
}

const ROWS: Row[] = [
  { symbol: "BBB", price: "9.50", as_of: "2026-02-01" },
  { symbol: "AAA", price: "10.25", as_of: "2026-01-01" },
  { symbol: "CCC", price: null, as_of: "2026-03-01" },
];

const COLUMNS: Column<Row>[] = [
  { key: "symbol", label: "Symbol" },
  { key: "price", label: "Price", align: "right" },
  { key: "as_of", label: "As of" },
];

afterEach(cleanup);

function bodyOrder(): string[] {
  const rows = screen.getAllByRole("row").slice(1);
  return rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

describe("sorting a table", () => {
  it("leaves the panel's own order alone until a heading is clicked", () => {
    render(<SortedTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.symbol} />);
    expect(bodyOrder()).toEqual(["BBB", "AAA", "CCC"]);
  });

  it("compares decimals as numbers, not as text", async () => {
    const user = userEvent.setup();
    render(<SortedTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.symbol} />);
    await user.click(screen.getByRole("button", { name: /Price/ }));
    // "9.50" is not after "10.25": the archive sends decimals as strings
    // and a text sort would put the bigger number first.
    expect(bodyOrder()).toEqual(["BBB", "AAA", "CCC"]);
    await user.click(screen.getByRole("button", { name: /Price/ }));
    expect(bodyOrder()).toEqual(["AAA", "BBB", "CCC"]);
  });

  it("puts an empty cell last whichever way the column points", async () => {
    const user = userEvent.setup();
    render(<SortedTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.symbol} />);
    await user.click(screen.getByRole("button", { name: /Price/ }));
    expect(bodyOrder()[2]).toBe("CCC");
    await user.click(screen.getByRole("button", { name: /Price/ }));
    // An absence is not the smallest number.
    expect(bodyOrder()[2]).toBe("CCC");
  });

  it("orders dates by time and says which way in aria-sort", async () => {
    const user = userEvent.setup();
    render(<SortedTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.symbol} />);
    await user.click(screen.getByRole("button", { name: /As of/ }));
    expect(bodyOrder()).toEqual(["AAA", "BBB", "CCC"]);
    expect(screen.getByRole("columnheader", { name: /As of/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("gives the rows back in the panel's order on the third click", async () => {
    const user = userEvent.setup();
    render(<SortedTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.symbol} />);
    const heading = screen.getByRole("button", { name: /Symbol/ });
    await user.click(heading);
    expect(bodyOrder()).toEqual(["AAA", "BBB", "CCC"]);
    await user.click(heading);
    expect(bodyOrder()).toEqual(["CCC", "BBB", "AAA"]);
    await user.click(heading);
    expect(bodyOrder()).toEqual(["BBB", "AAA", "CCC"]);
    expect(screen.getByRole("columnheader", { name: /Symbol/ })).not.toHaveAttribute("aria-sort");
  });

  it("puts a numeric heading over its own column", () => {
    render(<SortedTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.symbol} />);
    // Alignment must be on the heading too, or a right-aligned number sits
    // under the next column's left-aligned label.
    expect(screen.getByRole("columnheader", { name: /Price/ })).toHaveClass("num");
  });
});
