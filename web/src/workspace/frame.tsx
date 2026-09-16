// What a panel is allowed to know about the page around it: what happens
// when it runs a command, and whether it is the one the keyboard is
// talking to. `Workspace` gives each dockview panel its own frame; with
// no frame, the address bar is the frame. Two hooks rather than one:
// running a command needs the router, having the keyboard does not.
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
