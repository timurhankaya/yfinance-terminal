import { useEffect, useState } from "react";

/** Recompute SVG geometry at the panel's width, keeping type readable
 * and height bounded on wide monitors and in split workspaces. */
export function useChartSize(baseHeight = 260) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    if (host === null) return;
    const observer = new ResizeObserver(([entry]) => {
      const next = entry?.contentRect.width;
      if (next && next > 0) setWidth(Math.max(200, Math.round(next)));
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [host]);
  return { ref: setHost, width, height: Math.round(Math.max(baseHeight, Math.min(400, width * baseHeight / 640))) };
}
