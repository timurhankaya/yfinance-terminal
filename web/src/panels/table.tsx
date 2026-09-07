// The typed table every dataset panel renders through. Cell format comes
// from the catalogue's wire type, not from the panel: a decimal is a
// decimal whichever of the 56 datasets it sits in. Nothing is dropped:
// the one column the grid hides (raw_json, a blob the width of the page)
// is in the row detail, along with every other field of the row.
import { useEffect, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import type { CatalogColumn, Row } from "../api/client";
import { LOCALE, asNumber, formatBig } from "./DES";
import { DataTable, useListKeys, type Column } from "./common";

//: Shown only in the row detail: as a grid column it would be a page wide.
export const DETAIL_ONLY = new Set(["raw_json"]);

const HTTP_RE = /^https?:\/\//i;

export function isHttpUrl(value: unknown): value is string {
  return typeof value === "string" && HTTP_RE.test(value);
}

/** Decimals below a thousand keep two places (an EPS of 7.49, a ratio of
 *  0.16); above it the K/M/B/T scaler takes over, as in FA. */
export function formatDecimal(value: unknown): string {
  const n = asNumber(value);
  if (n === null) return "—";
  return Math.abs(n) < 1000 ? n.toFixed(2) : formatBig(n);
}

const YEAR_RE = /year|born/;

/** Thousands separators, except for a year (1,974 is not a year). */
export function formatInteger(value: unknown, name = ""): string {
  const n = asNumber(value);
  if (n === null) return "—";
  return YEAR_RE.test(name) ? String(n) : n.toLocaleString(LOCALE);
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
export function formatCell(value: unknown, type: string, name = ""): ReactNode {
  if (value === null || value === undefined) return "—";
  if (type === "string (decimal)") return formatDecimal(value);
  if (type === "integer") return formatInteger(value, name);
  if (type === "string (date-time)") return formatDateTime(value);
  if (type === "boolean") return value ? "yes" : "no";
  if (isHttpUrl(value)) {
    return (
      <a href={value} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
        {value}
      </a>
    );
  }
  return String(value);
}

export function isNumericType(type: string): boolean {
  return type === "string (decimal)" || type === "integer";
}

function prettyJson(value: unknown): string {
  if (typeof value !== "string") return String(value);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

/** Every field of one row, raw_json pretty-printed. */
export function RowDetail(props: { row: Row; columns: CatalogColumn[]; onClose: () => void }): ReactElement {
  const { row, columns, onClose } = props;
  const known = new Set(columns.map((c) => c.name));
  // Fields the catalogue did not list still show: a row is the truth,
  // the catalogue is a description of it.
  const extra = Object.keys(row).filter((k) => !known.has(k));
  return (
    <div className="detail" role="region" aria-label="row detail">
      <button type="button" className="fn" onClick={onClose}>
        Close
      </button>
      <dl className="des">
        {columns.map((column) => (
          <Field key={column.name} name={column.name} type={column.type} value={row[column.name]} />
        ))}
        {extra.map((name) => (
          <Field key={name} name={name} type="string" value={row[name]} />
        ))}
      </dl>
    </div>
  );
}

function Field({ name, type, value }: { name: string; type: string; value: unknown }): ReactElement {
  if (DETAIL_ONLY.has(name) && value !== null && value !== undefined) {
    return (
      <>
        <dt>{name}</dt>
        <dd>
          <pre className="json">{prettyJson(value)}</pre>
        </dd>
      </>
    );
  }
  return (
    <>
      <dt>{name}</dt>
      <dd>{formatCell(value, type, name)}</dd>
    </>
  );
}

export interface DatasetTableProps {
  columns: CatalogColumn[];
  rows: Row[];
  /** Columns to leave out of the grid (a symbol column that repeats the
   *  strip, say). They stay in the row detail. */
  hide?: string[];
  /** Newest first: the API sends most datasets newest first already, but
   *  bars and actions come oldest first. */
  reverse?: boolean;
  truncated?: boolean;
}

/** A dataset as a grid with j/k/Enter and click opening the row detail. */
export function DatasetTable(props: DatasetTableProps): ReactElement {
  const { columns, rows: given, hide = [], reverse = false, truncated = false } = props;
  const rows = reverse ? [...given].reverse() : given;
  const [open, setOpen] = useState<number | null>(null);
  const toggle = (index: number) => setOpen((current) => (current === index ? null : index));
  const [selected, setSelected] = useListKeys(rows.length, toggle);
  const tableRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const body = tableRef.current?.querySelector("tbody");
    const el = body?.children[selected];
    if (el instanceof HTMLElement && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest" });
    }
  }, [selected]);

  const hidden = new Set([...hide, ...DETAIL_ONLY]);
  const shown = columns.filter((c) => !hidden.has(c.name));
  const gridColumns: Column<Row>[] = shown.map((c) => ({
    key: c.name,
    label: c.name,
    align: isNumericType(c.type) ? "right" : "left",
    format: (row) => formatCell(row[c.name], c.type, c.name),
  }));
  const detail = open === null ? undefined : rows[open];

  return (
    <div className="dataset">
      <p className="detail-meta">
        {rows.length.toLocaleString(LOCALE)} rows{truncated ? " (more exist: the list was cut at the page cap)" : ""} ·{" "}
        {shown.length} of {columns.length} columns in the grid; Enter or click a row for every field
      </p>
      <div className="scroll-x" ref={tableRef}>
        <DataTable
          columns={gridColumns}
          rows={rows}
          rowKey={(_row, index) => String(index)}
          selected={selected}
          onSelect={(index) => {
            setSelected(index);
            toggle(index);
          }}
        />
      </div>
      {detail && <RowDetail row={detail} columns={columns} onClose={() => setOpen(null)} />}
    </div>
  );
}
