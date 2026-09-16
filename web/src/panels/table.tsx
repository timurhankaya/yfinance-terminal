// The typed table every dataset panel renders through. Cell format comes
// from the catalogue's wire type, not from the panel: a decimal is a
// decimal whichever dataset it sits in. Nothing is dropped:
// the one column the grid hides (raw_json, a blob the width of the page)
// is in the row detail, along with every other field of the row.
import { useEffect, useRef, useState, useMemo } from "react";
import type { ReactElement, ReactNode } from "react";
import { WireType, type CatalogColumn, type Row } from "../api/client";
import { DataTable, useListKeys, useSortedRows, type Column } from "./common";
import { Controls, RowFilter } from "./controls";
import { LOCALE, asNumber, formatBig, isHttpUrl, isProse } from "./format";
import type { LinkRule } from "./links";

//: Shown only in the row detail: as a grid column it would be a page wide.
export const DETAIL_ONLY = new Set(["raw_json"]);

/** Decimals below a thousand keep two places (an EPS of 7.49, a ratio of
 *  0.16); above it the K/M/B/T scaler takes over, as in FA. */
export function formatDecimal(value: unknown): string {
  const n = asNumber(value);
  if (n === null) return "—";
  // A fraction below one (an ownership share of 0.0165) would round to
  // 0.02 at two places; four places keep it honest. The raw string is
  // in the row detail regardless.
  if (Math.abs(n) < 1) return n.toFixed(4);
  return Math.abs(n) < 1000 ? n.toFixed(2) : formatBig(n);
}

/** The value as the API sent it, trailing zeros trimmed: the detail
 *  shows full precision where the grid rounds. */
export function rawDecimal(value: unknown): string {
  if (typeof value !== "string") return formatDecimal(value);
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

//: Integer years, by column name: 1,974 is not a year of birth.
const YEAR_COLUMNS = new Set(["year_born", "fiscal_year", "year"]);

/** Thousands separators, except for a year (1,974 is not a year). */
export function formatInteger(value: unknown, name = ""): string {
  const n = asNumber(value);
  if (n === null) return "—";
  return YEAR_COLUMNS.has(name) ? String(n) : n.toLocaleString(LOCALE);
}

/** An ISO date-time as `YYYY-MM-DD HH:MM UTC`, or the date alone at
 *  midnight. UTC, not the browser's zone: the archive keys everything by
 *  UTC and a terminal reads the same in every city. */
export function formatDateTime(value: unknown): string {
  if (typeof value !== "string") return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const iso = date.toISOString();
  return iso.endsWith("T00:00:00.000Z") ? iso.slice(0, 10) : `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/** One cell, by the catalogue's wire type. Strings that are URLs become
 *  links; everything null is a dash. */
export function formatCell(value: unknown, type: WireType, name = ""): ReactNode {
  if (value === null || value === undefined) return "—";
  if (type === WireType.Decimal) return formatDecimal(value);
  if (type === WireType.Integer) return formatInteger(value, name);
  if (type === WireType.DateTime) return formatDateTime(value);
  if (type === WireType.Boolean) return value ? "yes" : "no";
  if (isHttpUrl(value)) {
    return (
      <a href={value} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
        {value}
      </a>
    );
  }
  const text = String(value);
  // The grid clips long text with an ellipsis (styles.css); the title
  // attribute keeps the whole value a hover away, and the row detail has
  // it in full.
  return text.length > 40 ? <span title={text}>{text}</span> : text;
}

export function isNumericType(type: WireType): boolean {
  return type === WireType.Decimal || type === WireType.Integer;
}

function prettyJson(value: unknown): string {
  if (typeof value !== "string") return String(value);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function Links({ row, rules }: { row: Row; rules: LinkRule[] }): ReactElement | null {
  const items = rules.flatMap((rule) => {
    const href = rule.href(row);
    return href ? [[rule.label, href] as const] : [];
  });
  if (items.length === 0) return null;
  return (
    <span className="row-links">
      {items.map(([label, href]) => (
        <a key={href} href={href} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
          {label} ↗
        </a>
      ))}
    </span>
  );
}

/** Every field of one row, including the ones the grid did not draw --
 *  this is where a wide dataset actually lives. Same field grid as DES's
 *  snapshot tabs. Blobs are pulled out of the grid and given the full
 *  width below it: pretty-printed JSON in a narrow cell is a column of
 *  single characters. */
export function RowDetail(props: {
  row: Row;
  columns: CatalogColumn[];
  links?: LinkRule[];
  /** What this row is, for the heading -- the first grid column's value,
   *  which is the one the reader clicked on. */
  title?: string;
  onClose: () => void;
}): ReactElement {
  const { row, columns, links = [], title, onClose } = props;
  const known = new Set(columns.map((c) => c.name));
  // Fields the catalogue did not list still show: a row is the truth,
  // the catalogue is a description of it.
  const extra = Object.keys(row).filter((k) => !known.has(k));
  const all: Array<[name: string, type: WireType]> = [
    ...columns.map((column): [string, WireType] => [column.name, column.type]),
    ...extra.map((name): [string, WireType] => [name, WireType.String]),
  ];
  const filled = (name: string) => row[name] !== null && row[name] !== undefined;
  const blobs = all.filter(([name]) => DETAIL_ONLY.has(name) && filled(name));
  const plain = all.filter(([name]) => !DETAIL_ONLY.has(name));

  return (
    <div className="detail" role="region" aria-label="row detail">
      <div className="detail-head">
        <h4>{title === undefined || title === "" ? "Row" : title}</h4>
        {links.length > 0 && <Links row={row} rules={links} />}
        <button type="button" className="detail-close" onClick={onClose}>
          Close
        </button>
      </div>
      <dl className="fields">
        {plain.map(([name, type]) => (
          <Field key={name} name={name} type={type} value={row[name]} />
        ))}
      </dl>
      {blobs.map(([name]) => (
        <section className="fieldset" key={name}>
          <h4>{name}</h4>
          <pre className="json">{prettyJson(row[name])}</pre>
        </section>
      ))}
    </div>
  );
}

function Field({ name, type, value }: { name: string; type: WireType; value: unknown }): ReactElement {
  // Full precision here, the grid's rounding there: the detail is where
  // a reader comes to check a number, and 0.66435 is not 0.6644.
  const shown = type === WireType.Decimal && value !== null && value !== undefined
    ? rawDecimal(value)
    : formatCell(value, type, name);
  return (
    // From the RAW value: `formatCell` may return an element.
    <div className={isProse(value) ? "field field-prose" : "field"}>
      <dt className="field-key" title={name}>{name}</dt>
      {/* The tooltip only where the cell is text: `formatCell` returns
          a link element for a URL, and a title of "[object Object]"
          would be worse than none. */}
      <dd className="field-val" title={typeof shown === "string" ? shown : undefined}>{shown}</dd>
    </div>
  );
}

/** What the detail pane calls the row: its first grid column, as text. */
function detailTitle(row: Row, shown: CatalogColumn[]): string | undefined {
  const first = shown[0];
  if (first === undefined) return undefined;
  const value = formatCell(row[first.name], first.type, first.name);
  return typeof value === "string" && value !== "—" ? value : undefined;
}

export interface DatasetTableProps {
  columns: CatalogColumn[];
  rows: Row[];
  /** The columns the GRID draws, by name and in order; absent means every
   *  column the catalogue lists. The row detail shows all of them either
   *  way. The rule lives in `grid.ts` and arrives here worked out, so
   *  there is one place to look when a column goes missing. */
  grid?: string[];
  /** Newest first: the API sends most datasets newest first already, but
   *  bars and actions come oldest first. */
  reverse?: boolean;
  truncated?: boolean;
  /** Links the rows imply (a report page, a quote page); shown as an
   *  "open" column and in the row detail. */
  links?: LinkRule[];
  /** Present when another page can be fetched: renders the Load more
   *  button, which calls it. */
  onLoadMore?: () => void;
  loadingMore?: boolean;
  pageControl?: ReactNode;
}

/** Rows containing the text, anywhere in any column the reader can see.
 *  This narrows what is loaded, not what the archive returns: the API
 *  filters by column, not free text. `RowFilter` says which it is. */
function narrow(rows: Row[], query: string): Row[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return rows;
  return rows.filter((row) =>
    Object.entries(row).some(
      ([key, value]) =>
        key !== "raw_json" && value !== null && String(value).toLowerCase().includes(needle),
    ),
  );
}

/** A dataset as a grid with j/k/Enter and click opening the row detail. */
export function DatasetTable(props: DatasetTableProps): ReactElement {
  const { columns, rows: given, grid, reverse = false, truncated = false, links = [], onLoadMore, loadingMore = false, pageControl } = props;
  const ordered = reverse ? [...given].reverse() : given;
  const [query, setQuery] = useState("");
  // Narrowing before ordering, and both before j/k and the row detail
  // count from them: the keyboard has to walk the rows the reader is
  // actually looking at.
  const narrowed = useMemo(() => narrow(ordered, query), [ordered, query]);
  const { rows, sort, toggle: sortBy } = useSortedRows(narrowed);
  const [open, setOpen] = useState<number | null>(null);
  const toggle = (index: number) => setOpen((current) => (current === index ? null : index));
  const [selected, setSelected] = useListKeys(rows.length, toggle);
  const tableRef = useRef<HTMLDivElement>(null);
  //: Mount is not a move: the effect below follows j/k, and on the first
  //: run there has been none. Scrolling row 0 into view on mount throws
  //: the reader past everything above a table that is not at the top of
  //: its panel.
  const moved = useRef(false);

  useEffect(() => {
    if (!moved.current) {
      moved.current = true;
      return;
    }
    const body = tableRef.current?.querySelector("tbody");
    const el = body?.children[selected];
    if (el instanceof HTMLElement && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest" });
    }
  }, [selected]);

  const byName = new Map(columns.map((column) => [column.name, column]));
  const shown = grid === undefined
    ? columns.filter((column) => !DETAIL_ONLY.has(column.name))
    // In the caller's order, and only the columns the catalogue has.
    : grid.flatMap((name) => {
        const column = byName.get(name);
        return column === undefined || DETAIL_ONLY.has(name) ? [] : [column];
      });
  const gridColumns: Column<Row>[] = shown.map((c) => ({
    key: c.name,
    label: c.name,
    align: isNumericType(c.type) ? "right" : "left",
    format: (row) => formatCell(row[c.name], c.type, c.name),
  }));
  // After the catalogue's, so the grid's own columns keep the order the
  // dataset declares them in.
  if (links.length > 0) {
    // First, not last: a wide grid scrolls sideways and the links would be off-screen.
    gridColumns.unshift({ key: "__links", label: "open", format: (row) => <Links row={row} rules={links} /> });
  }
  const detail = open === null ? undefined : rows[open];

  const more = onLoadMore !== undefined;
  // The detail opens on the right, like the news article: the grid keeps
  // its place and its selection, and the row's fields sit beside it.
  return (
    <div className={detail ? "dataset split" : "dataset"}>
      <div className={detail ? "split-list" : undefined}>
        <Controls>
          <RowFilter
            value={query}
            onChange={(next) => {
              setQuery(next);
              // The detail was open on a row that may not be in the
              // narrowed list any more.
              setOpen(null);
            }}
            count={rows.length}
            total={ordered.length}
          />
        </Controls>
        <p className="detail-meta">
          {rows.length.toLocaleString(LOCALE)} {rows.length === 1 ? "row" : "rows"}
          {more ? " loaded, more available" : ""}
          {truncated ? " (more exist: the list was cut at the page cap)" : ""} ·{" "}
          {shown.length} of {columns.length} columns in the grid; Enter or click a row for every field
        </p>
        {rows.length === 0 && <p className="table-empty" role="status">{query.trim() ? "No loaded rows match this filter. Clear it or load more records." : "No records for these filters. Adjust the dataset filters to try again."}</p>}
        <div className="scroll-x" ref={tableRef}>
          <DataTable
            columns={gridColumns}
            rows={rows}
            sort={sort}
            onSort={sortBy}
            rowKey={(_row, index) => String(index)}
            selected={selected}
            onSelect={(index) => {
              setSelected(index);
              toggle(index);
            }}
          />
        </div>
        <div className="table-footer" aria-label="Table pagination">
          <span className="table-total" role="status">{ordered.length.toLocaleString(LOCALE)} {ordered.length === 1 ? "row" : "rows"} loaded{more ? " · More available" : truncated ? " · Page cap reached" : " · All loaded"}</span>
          <div className="table-page-actions">
            {pageControl}
            {more && <button type="button" className="fn load-more-button" disabled={loadingMore} onClick={onLoadMore}>{loadingMore ? "Loading…" : "Load more"}</button>}
          </div>
        </div>
      </div>
      {detail && (
        <div className="split-detail">
          <RowDetail
            row={detail}
            columns={columns}
            links={links}
            // The first grid column of the row the reader clicked: the
            // holder's name, the date, the contract. A heading of "Row
            // detail" says nothing they did not already know.
            title={detailTitle(detail, shown)}
            onClose={() => setOpen(null)}
          />
        </div>
      )}
    </div>
  );
}
