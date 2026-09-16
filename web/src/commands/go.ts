// One way to run a command, so one place knows how a command becomes a
// history entry. A market page's address carries no symbol, so the
// strip's symbol rides in the history entry: Esc and forward restore it
// per step, and a pasted link carries none.
import { useCallback } from "react";
import { useNavigate } from "react-router";
import { commandToPath } from "./parser";
import type { Command } from "./types";

/** Navigates to a command, carrying the strip's symbol with it. */
export function useGo(): (command: Command) => void {
  const navigate = useNavigate();
  return useCallback(
    (command: Command) => {
      void navigate(commandToPath(command), { state: { symbol: command.symbol } });
    },
    [navigate],
  );
}
