// How to get a second panel, in the place a reader looks first.
//
// The gestures existed before this block did; nothing on screen said so.
// It is three lines, not a tour: a terminal reader reads once and then
// uses the keyboard.
import { Block } from "./Block";

export function Guide() {
  return (
    <Block title="Build a page">
      {() => (
        <ul className="list home-guide">
          <li className="list-row">
            <code className="usage">+</code> on a panel&apos;s tab bar, or{" "}
            <code className="usage">Ctrl+Enter</code> in the command box — a second panel beside
            this one. <code className="usage">Ctrl+Shift+arrow</code> moves the keyboard between
            them.
          </li>
          <li className="list-row">
            <code className="usage">GRP A</code> pins a panel to a letter. Type a symbol and every
            panel wearing that letter follows it.
          </li>
          <li className="list-row">
            <code className="usage">PG SAVE trading</code> keeps the layout under a name, on the
            next function key. <code className="usage">PG</code> lists what is saved,{" "}
            <code className="usage">SHARE</code> turns it into a link.
          </li>
        </ul>
      )}
    </Block>
  );
}
