// One tile of the home grid: a heading that opens the panel it stands
// for, and a body that says which of loading, empty, failed or ready it
// is. Separate reads: one slow or broken block must not blank the page.
import { useRef } from "react";
import type { ReactNode, RefObject } from "react";
import { useWhenVisible } from "./visible";

export function Block(props: {
  title: string;
  /** What the heading opens. Absent leaves the heading as plain text. */
  onOpen?: () => void;
  /** Where the block's own read got to, as a line under the heading. */
  note?: ReactNode;
  wide?: boolean;
  children: (visible: boolean) => ReactNode;
}) {
  const { title, onOpen, note, wide = false, children } = props;
  const host = useRef<HTMLElement>(null);
  const visible = useWhenVisible(host as RefObject<Element | null>);

  return (
    <section className={wide ? "home-block home-block-wide" : "home-block"} ref={host}>
      <h3 className="home-title">
        {onOpen === undefined ? (
          title
        ) : (
          <button type="button" className="help-fn" onClick={onOpen}>
            {title}
          </button>
        )}
        {note !== undefined && <span className="home-note">{note}</span>}
      </h3>
      {children(visible)}
    </section>
  );
}

/** The line a block shows while it has nothing yet. */
export function Waiting({ what }: { what: string }) {
  return <p className="muted">Loading {what}…</p>;
}

/** A block that could not read. Said in the block, never as the page:
 *  the other five have their own answers. */
export function Failed({ what, message }: { what: string; message: string }) {
  return (
    <p className="card card-error">
      {what}: {message}
    </p>
  );
}
