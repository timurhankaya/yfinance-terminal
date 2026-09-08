import { describe, expect, it } from "vitest";
import { WireType, type CatalogColumn } from "../api/client";
import { GRID, HOUSEKEEPING, gridNames } from "./grid";

function columns(...names: string[]): CatalogColumn[] {
  return names.map((name) => ({ name, type: WireType.String, nullable: true }));
}

describe("gridNames", () => {
  it("uses the dataset's own list, in its order", () => {
    const shown = gridNames(
      "institutional_holders",
      columns("symbol", "as_of_date", "holder_type", "holder", "date_reported", "pct_held", "pct_change", "shares", "value", "fetched_at"),
    );
    expect(shown).toEqual(["holder", "date_reported", "pct_held", "pct_change", "shares", "value"]);
  });

  it("keeps fetched_at where it is the time axis rather than bookkeeping", () => {
    // The rule that a blanket housekeeping filter would have got wrong:
    // on a `*_history` table `fetched_at` IS the row's identity.
    const cols = columns("symbol", "fetched_at", "last_price", "previous_close", "day_low", "day_high", "market_cap", "last_volume", "content_hash");
    expect(gridNames("fast_info_history", cols)[0]).toBe("fetched_at");
    expect(gridNames("fast_info", cols)).not.toContain("fetched_at");
  });

  it("falls back to every column but the housekeeping, minus what the panel drops", () => {
    const cols = columns("symbol", "as_of_date", "shares", "fetched_at", "raw_json", "content_hash", "is_known");
    expect(gridNames("not_in_the_registry", cols)).toEqual(["symbol", "as_of_date", "shares"]);
    expect(gridNames("not_in_the_registry", cols, ["symbol"])).toEqual(["as_of_date", "shares"]);
  });

  it("never names a column the catalogue does not have", () => {
    // A column the archive renames must leave a gap, not a heading over
    // an empty column.
    expect(gridNames("institutional_holders", columns("holder", "shares"))).toEqual(["holder", "shares"]);
  });

  it("keeps every grid short enough to read across", () => {
    // The whole point of the registry: past seven columns a reader is
    // scrolling sideways again.
    for (const [dataset, names] of Object.entries(GRID)) {
      expect(names.length, dataset).toBeGreaterThan(0);
      expect(names.length, dataset).toBeLessThanOrEqual(8);
      expect(new Set(names).size, dataset).toBe(names.length);
      for (const name of names) {
        // `fetched_at` is the one housekeeping column a dataset may ask
        // for back, and only the history tables do.
        if (name === "fetched_at") expect(dataset, dataset).toMatch(/_history$/);
        else expect(HOUSEKEEPING.has(name), `${dataset}.${name}`).toBe(false);
      }
    }
  });
});
