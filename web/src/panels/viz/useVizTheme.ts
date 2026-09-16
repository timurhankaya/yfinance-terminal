// The palette, as something React re-renders for. `vizTheme()` caches
// the stylesheet read; this subscribes to the reset, so picking an
// accent redraws everything that draws with it.
import { useSyncExternalStore } from "react";
import { subscribeVizTheme, vizTheme } from "./colors";
import type { VizTheme } from "./colors";

export function useVizTheme(): VizTheme {
  return useSyncExternalStore(subscribeVizTheme, vizTheme, vizTheme);
}
