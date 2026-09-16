// How to get a second panel, in the place a reader looks first.
//
// It is three lines, not a tour: a terminal reader reads once and then
// uses the keyboard.
import { Block } from "./Block";
import { dockviewEnabled } from "../../app/config";

export function Guide() {
  if (!dockviewEnabled()) return null;
  return (
    <Block title="Build a page">
      {() => (
        <ul className="list home-guide">
          <li className="list-row">
            <code className="usage">+</code> opens a workspace with a second panel beside this page.
            Inside a workspace, <code className="usage">Ctrl+Enter</code> adds another panel. <code className="usage">Ctrl+Shift+arrow</code> moves the keyboard between
            them.
          </li>
          <li className="list-row">
            <code className="usage">GRP A</code> pins a panel to a letter. Type a symbol and every
            panel wearing that letter follows it.
          </li>
          <li className="list-row">
            <code className="usage">SHARE</code> turns the workspace into a link.
          </li>
        </ul>
      )}
    </Block>
  );
}
