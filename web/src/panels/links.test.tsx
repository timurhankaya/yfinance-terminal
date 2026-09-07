import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { CatalogColumn } from "../api/client";
import { linksFor, quoteUrl } from "./links";
import { DatasetTable } from "./table";

afterEach(cleanup);

describe("linksFor", () => {
  it("derives report, sector, screener and quote pages from the row", () => {
    const report = linksFor("research_reports", ["report_id"]);
    expect(report.map((r) => r.label)).toEqual(["report"]);
    expect(report[0]!.href({ report_id: "ARGUS_3630_AnalystReport_1784891224000" })).toBe(
      "https://finance.yahoo.com/research/reports/ARGUS_3630_AnalystReport_1784891224000",
    );

    const [sector] = linksFor("domains", ["domain_key", "parent_key", "symbol"]);
    expect(sector!.href({ domain_key: "technology", parent_key: null })).toBe("https://finance.yahoo.com/sectors/technology/");
    expect(sector!.href({ domain_key: "consumer-electronics", parent_key: "technology" })).toBe(
      "https://finance.yahoo.com/sectors/technology/consumer-electronics/",
    );

    const [edgar] = linksFor("sec_filings", ["filing_id", "symbol"]);
    expect(edgar!.href({ filing_id: "0001140361-26-035325_320193" })).toBe(
      "https://www.sec.gov/Archives/edgar/data/320193/000114036126035325/",
    );
    expect(edgar!.href({ filing_id: "0001140361-26-035325" })).toBeNull();
    expect(edgar!.href({ filing_id: "junk_1" })).toBeNull();

    const quote = linksFor("major_holders", ["symbol", "as_of_date"]);
    expect(quote.map((r) => r.label)).toEqual(["quote"]);
    expect(quote[0]!.href({ symbol: "BRK-B" })).toBe("https://finance.yahoo.com/quote/BRK-B/");
    expect(quote[0]!.href({ symbol: null })).toBeNull();
    expect(quoteUrl("^GSPC")).toBe("https://finance.yahoo.com/quote/%5EGSPC/");
  });

  it("gives a dataset without any rule no links", () => {
    expect(linksFor("economic_calendar", ["region", "event_name"])).toEqual([]);
  });
});

describe("DatasetTable links", () => {
  const columns: CatalogColumn[] = [
    { name: "report_id", type: "string", nullable: false },
    { name: "provider", type: "string", nullable: true },
  ];
  const rows = [{ report_id: "X_1", provider: "Argus" }, { report_id: null, provider: "None" }];

  it("adds an open column and repeats the links in the row detail", () => {
    render(<DatasetTable columns={columns} rows={rows} links={linksFor("research_reports", ["report_id"])} />);
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["open", "report_id", "provider"]);
    const [, first, second] = screen.getAllByRole("row");
    expect(within(first!).getByRole("link", { name: "report ↗" }).getAttribute("href")).toBe(
      "https://finance.yahoo.com/research/reports/X_1",
    );
    expect(within(second!).queryByRole("link")).toBeNull();
    fireEvent.click(first!);
    const detail = screen.getByRole("region", { name: "row detail" });
    expect(within(detail).getByRole("link", { name: "report ↗" })).toBeTruthy();
  });

  it("adds no column when there are no rules", () => {
    render(<DatasetTable columns={columns} rows={rows} />);
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["report_id", "provider"]);
  });
});
