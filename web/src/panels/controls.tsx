// The arguments, as things to click.
//
// Every one of these edits the SAME args the command line edits: a
// control calls `run` with the panel's own code and the arg it changed,
// so the address (or the saved page) is still the only place the state
// lives. Nothing here holds a value of its own -- except the text box,
// which holds what is being typed until it is worth a new request.
//
// That is why they are not "filters" or "settings": `PX 5m 500` and
// clicking 5m then typing 500 are the same command, and HELP still
// documents one syntax.
import { useEffect, useId, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import type { PanelArgs } from "../commands/types";
import { usePanelRun } from "../workspace/frame";

/** Changes one or more args of the panel this is rendered in. */
export function useArgs(
  code: string,
  symbol: string | null,
  args: PanelArgs,
): (next: PanelArgs) => void {
  const run = usePanelRun();
  return (next: PanelArgs) => run({ symbol, code, args: Object.fromEntries(Object.entries({ ...args, ...next }).filter(([, value]) => value !== "")) });
}

/** A row of controls above a panel's body. */
export function Controls({ children }: { children: ReactNode }): ReactElement {
  return (
    <div className="controls" role="group" aria-label="options">
      {children}
    </div>
  );
}

/** One of a fixed set: an interval, a period, a statement.
 *
 *  Buttons rather than a `<select>`: there are rarely more than seven and
 *  a terminal reader is picking between things they already know the
 *  names of, so the choice should be visible, not behind a click. */
export function Choice<T extends string>(props: {
  label: string;
  value: T;
  options: readonly T[];
  onPick: (value: T) => void;
  format?: (value: T) => string;
}): ReactElement {
  const { label, value, options, onPick, format } = props;
  return (
    <span className="control">
      <span className="control-label">{label}</span>
      <span className="choice" role="group" aria-label={label}>
        {options.map((option) => (
          <button
            key={option}
            type="button"
            className={option === value ? "chip chip-on" : "chip"}
            aria-pressed={option === value}
            onClick={() => onPick(option)}
          >
            {format ? format(option) : option}
          </button>
        ))}
      </span>
    </span>
  );
}

/** A whole number inside a range: rows, years, a page size.
 *
 *  Committed on Enter or on leaving the field, not on every keystroke: a
 *  reader typing 500 would otherwise fetch 5, then 50, then 500. */
export function NumberArg(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  onSet: (value: number) => void;
  suffix?: string;
}): ReactElement {
  const { label, value, min, max, onSet, suffix } = props;
  const id = useId();
  const [draft, setDraft] = useState(String(value));
  //: Enter commits and then leaves the field, and leaving commits too --
  //: so what was just sent is remembered, or a single Enter would ask
  //: for the same thing twice.
  const sent = useRef(value);
  useEffect(() => {
    setDraft(String(value));
    sent.current = value;
  }, [value]);

  const commit = () => {
    const next = Number(draft);
    if (!Number.isInteger(next) || next < min || next > max) {
      setDraft(String(value));
      return;
    }
    if (next === value || next === sent.current) return;
    sent.current = next;
    onSet(next);
  };

  return (
    <span className="control control-field control-number-field">
      <label className="control-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="control-number"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={draft}
        aria-label={`${label} (${min}-${max})`}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            event.stopPropagation();
            commit();
            event.currentTarget.blur();
          }
        }}
      />
      {suffix !== undefined && <span className="control-label">{suffix}</span>}
    </span>
  );
}

/** A free-text argument: a filter value, a filing type, a query.
 *
 *  Also committed on Enter or blur. An empty box means "no filter" and
 *  removes the argument rather than sending an empty one, which the API
 *  would refuse. */
export function TextArg(props: {
  label: string;
  value: string;
  onSet: (value: string) => void;
  placeholder?: string;
  type?: "text" | "date";
}): ReactElement {
  const { label, value, onSet, placeholder, type = "text" } = props;
  const id = useId();
  const [draft, setDraft] = useState(value);
  const sent = useRef(value);
  useEffect(() => {
    setDraft(value);
    sent.current = value;
  }, [value]);

  const commit = () => {
    const next = draft.trim();
    // Enter commits and blurs, and blur commits: without this a single
    // Enter would send the same value twice.
    if (next === value || next === sent.current) return;
    sent.current = next;
    onSet(next);
  };

  return (
    <span className="control control-field">
      <label className="control-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={type === "date" ? "control-date" : "control-text"}
        type={type}
        value={draft}
        placeholder={placeholder}
        aria-label={label}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            event.stopPropagation();
            commit();
            event.currentTarget.blur();
          }
        }}
      />
    </span>
  );
}

/** Narrows what is already on screen. Unlike the others this is NOT an
 *  argument: it filters the rows the panel has loaded rather than asking
 *  for different ones, and it says so, because a reader who thinks they
 *  searched the archive would be reading a subset and not know it. */
export function RowFilter(props: {
  value: string;
  onChange: (value: string) => void;
  count: number;
  total: number;
}): ReactElement {
  const { value, onChange, count, total } = props;
  const id = useId();
  return (
    <span className="control control-field">
      <label className="control-label" htmlFor={id}>
        Filter loaded rows
      </label>
      <input
        id={id}
        className="control-text"
        type="search"
        value={value}
        placeholder="Search within loaded rows…"
        aria-label="filter the rows already loaded"
        onChange={(event) => onChange(event.target.value)}
      />
      {value !== "" && <button type="button" className="chip filter-clear" onClick={() => onChange("")} aria-label="Clear row filter">Clear</button>}
      <span className="control-label filter-count" role="status">{value !== "" ? `${count} of ${total}` : `${total} loaded`}</span>
    </span>
  );
}

/** Server filters are applied as one query; typing never unmounts the form. */
export function DatasetFilters({ fields, values, onApply }: {
  fields: { name: string; type: "text" | "date" }[];
  values: PanelArgs;
  onApply: (values: PanelArgs) => void;
}): ReactElement | null {
  const id = useId();
  const [draft, setDraft] = useState(values);
  if (fields.length === 0) return null;
  const active = fields.filter(({ name }) => values[name]?.trim()).length;
  return (
    <details className="dataset-filters">
      <summary>Dataset filters <span className="filter-badge">{active > 0 ? `${active} active` : "All records"}</span></summary>
      <form aria-label="Dataset filters" onSubmit={(event) => {
        event.preventDefault();
        onApply(Object.fromEntries(fields.map(({ name }) => [name, (draft[name] ?? "").trim()])));
      }}>
        <div className="filter-fields">
          {fields.map(({ name, type }) => (
            <label className="control control-field" key={name} htmlFor={`${id}-${name}`}>
              <span className="control-label">{name.replaceAll("_", " ")}</span>
              <input id={`${id}-${name}`} className={type === "date" ? "control-date" : "control-text"} type={type} aria-label={name} value={draft[name] ?? ""} placeholder="Any" onChange={(event) => setDraft({ ...draft, [name]: event.target.value })} />
            </label>
          ))}
        </div>
        <div className="filter-actions">
          <span className="muted">Filters apply to all records.</span>
          <button type="button" className="chip" onClick={() => {
            const cleared = Object.fromEntries([...new Set([...Object.keys(values), ...fields.map(({ name }) => name)])].map((name) => [name, ""]));
            setDraft(cleared);
            onApply(cleared);
          }}>Clear filters</button>
          <button type="submit" className="chip chip-on">Apply filters</button>
        </div>
      </form>
    </details>
  );
}
