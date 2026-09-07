// One way to run a command, so one place knows how a command becomes a
// history entry.
//
// A market page's address carries no symbol -- `/ui/m/EQS` is not
// AAPL's screener -- but the strip's symbol has to survive a trip
// through one, or `AAPL DES`, `EQS`, `FA` would lose AAPL half-way. So
// it rides in the history entry: the right lifetime, because Esc and
// forward restore the symbol that was on the strip at that step, and a
// link someone pastes carries none.
//
// Threading that through every `navigate` call site would be the same
// decision repeated a dozen times, and the one that forgot would be a
// panel that silently drops the symbol.
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
