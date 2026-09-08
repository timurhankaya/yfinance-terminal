// Every field of the snapshot, four tabs down from the overview.
//
// The promise the terminal made was that a key Yahoo sent is a key the
// reader can find (`DES.tsx`'s opening comment, and it still holds): the
// tabs re-shelve the sections, they do not drop any. What changed is the
// shape -- one column of a hundred and fifty rows became a tab of a few
// multi-column blocks, so a field is found by looking rather than by
// scrolling.
import { Fragment } from "react";
import type { ReactElement, ReactNode } from "react";
import { WireType, getDataset, type CatalogColumn, type Row } from "../../api/client";
import { ErrorCard, LoadState, usePanelData } from "../common";
import { isHttpUrl, isProse } from "../format";
import { DatasetTable } from "../table";
import { formatInfo, label, type Entry } from "./info";
import { FieldTab, type TabView } from "./layout";

function InfoValue({ name, value }: { name: string; value: unknown }): ReactNode {
  if (isHttpUrl(value)) {
    return (
      <a href={value} target="_blank" rel="noopener noreferrer">
        {value}
      </a>
    );
  }
  return formatInfo(name, value);
}

/** One section's fields, as a grid that fills across before it fills
 *  down. `<div>` wrappers inside the `<dl>`, which the HTML content model
 *  allows and which is what lets a term and its value share one grid
 *  cell -- bare `dt`/`dd` children would be laid out as two independent
 *  items and the pairing would be lost. */
function FieldGrid({ entries }: { entries: Entry[] }): ReactElement {
  return (
    <dl className="fields">
      {entries.map(([key, value]) => (
        <div className={isProse(value) ? "field field-prose" : "field"} key={key}>
          <dt className="field-key" title={key}>{label(key)}</dt>
          <dd className="field-val" title={formatInfo(key, value)}>
            <InfoValue name={key} value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

const OFFICER_COLUMNS: CatalogColumn[] = [
  { name: "name", type: WireType.String, nullable: false },
  { name: "title", type: WireType.String, nullable: true },
  { name: "age", type: WireType.Integer, nullable: true },
  { name: "year_born", type: WireType.Integer, nullable: true },
  { name: "fiscal_year", type: WireType.Integer, nullable: true },
  { name: "total_pay", type: WireType.Decimal, nullable: true },
  { name: "exercised_value", type: WireType.Decimal, nullable: true },
  { name: "unexercised_value", type: WireType.Decimal, nullable: true },
];

/** The officers, under Reference and nowhere else.
 *
 *  Rendered by the tab rather than by the panel, which means it is
 *  fetched by the tab: a reader who came for the price no longer pays a
 *  request for a table they did not open. */
function Officers({ symbol }: { symbol: string }): ReactElement | null {
  const { state, retry } = usePanelData<Row[]>(
    `officers|${symbol}`,
    () => getDataset("company_officers", symbol),
    (rows) => rows.length === 0,
  );
  if (state.kind === LoadState.Loading) return <p className="muted">Loading officers…</p>;
  if (state.kind === LoadState.Error) return <ErrorCard message={state.message} onRetry={retry} />;
  if (state.kind !== LoadState.Ready) return null;
  return (
    <section className="fieldset">
      <h4>Officers</h4>
      <DatasetTable columns={OFFICER_COLUMNS} rows={state.data} />
    </section>
  );
}

export interface FieldsProps {
  symbol: string;
  tabs: TabView[];
  active: FieldTab;
  onPick: (tab: FieldTab) => void;
  /** How many keys the snapshot carried with nothing in them. */
  nulls: number;
  /** False when the symbol has never been synced, which is a different
   *  thing from a snapshot whose fields are all empty. */
  synced: boolean;
}

export function Fields(props: FieldsProps): ReactElement {
  const { symbol, tabs, active, onPick, nulls, synced } = props;
  const current = tabs.find((tab) => tab.key === active) ?? tabs[0];

  return (
    <section className="des-fields">
      <div className="subtabs" role="tablist" aria-label="snapshot fields">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={tab.key === active}
            className={tab.key === active ? "subtab subtab-on" : "subtab"}
            onClick={() => onPick(tab.key)}
          >
            {tab.label}
            <span className="subtab-count">{tab.fields}</span>
          </button>
        ))}
      </div>

      {current !== undefined && current.sections.length === 0 && (
        <p className="muted">This snapshot has no {current.label.toLowerCase()} fields.</p>
      )}
      {current?.sections.map(([title, entries]) => (
        <Fragment key={title}>
          <section className="fieldset">
            <h4>{title}</h4>
            <FieldGrid entries={entries} />
          </section>
        </Fragment>
      ))}
      {active === FieldTab.Reference && <Officers symbol={symbol} />}

      {synced ? (
        <p className="muted des-foot">
          {nulls > 0 ? `${nulls} empty fields not shown. ` : ""}
          The full snapshot history is under REF infohist.
        </p>
      ) : (
        <p className="muted des-foot">Never synced: run yfin sync --symbols {symbol}</p>
      )}
    </section>
  );
}
