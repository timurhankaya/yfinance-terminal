// What a panel is allowed to know about the page around it.
//
// A panel used to reach `useNavigate` through `useGo` and change the
// address of the whole application. With one panel on screen that was the
// same thing as "replace what I am showing"; with four it is not --
// pressing Enter on an EQS row would abandon the page. The same was true
// of the keyboard: `useListKeys` listened on `window`, so four open lists
// all moved on one `j`.
//
// So a panel runs inside a frame, and the frame answers two questions:
// what happens when this panel runs a command, and is this panel the one
// the keyboard is talking to. `Workspace` gives each dockview panel its
// own frame. With no frame around it -- a page whose only panel is the
// address itself, and a panel rendered bare in a test -- the address bar
// is the frame, which is exactly what such a page's frame is.
//
// The two questions are asked through two hooks rather than one, because
// they do not cost the same: knowing whether you have the keyboard is
// free, while running a command with no frame around it needs the router.
// A panel that only reads lists must not be dragged into a <Router> for
// an answer it could have had for nothing.
import { createContext, useContext, useMemo } from "react";
import { useGo } from "../commands/go";
import type { Command } from "../commands/types";

export interface PanelFrame {
  /** Runs a command *in this panel*: replaces what it shows. */
  run: (command: Command) => void;
  /** Whether the keyboard is talking to this panel. */
  focused: boolean;
}

const FrameContext = createContext<PanelFrame | null>(null);

export const FrameProvider = FrameContext.Provider;

/** Whether this panel has the keyboard. Alone on a page, it always does. */
export function usePanelFocus(): boolean {
  return useContext(FrameContext)?.focused ?? true;
}

/** Runs a command in this panel, or -- with no frame -- goes there. */
export function usePanelRun(): (command: Command) => void {
  const provided = useContext(FrameContext);
  // `useGo` stays the one place that knows how a command becomes a
  // history entry; a page whose only panel is its address runs commands
  // by going to them.
  const go = useGo();
  return useMemo(() => provided?.run ?? go, [provided, go]);
}
