// The palette, as something React re-renders for.
//
// `vizTheme()` reads the stylesheet once and caches it, which is what
// two hundred sparklines need. This subscribes to the reset, so the one
// moment the cache is dropped -- the reader picking an accent -- redraws
// everything that draws with it.
import { useSyncExternalStore } from "react";
import { subscribeVizTheme, vizTheme } from "./colors";
import type { VizTheme } from "./colors";

export function useVizTheme(): VizTheme {
  return useSyncExternalStore(subscribeVizTheme, vizTheme, vizTheme);
}
