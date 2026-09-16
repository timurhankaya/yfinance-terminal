// Whether a block has been scrolled to, so a reader in a narrow dock
// panel pays only for what they can see. Once seen, always seen: a block
// that scrolls back out keeps its rows.
import { useEffect, useState } from "react";
import type { RefObject } from "react";

export function useWhenVisible(ref: RefObject<Element | null>): boolean {
  // jsdom has no IntersectionObserver, and neither do a few older
  // browsers. Both get the honest fallback: everything is visible, which
  // is the behaviour before this existed.
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === "undefined");

  useEffect(() => {
    if (seen) return;
    const element = ref.current;
    if (element === null) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setSeen(true);
      },
      // A little before it arrives, so the rows are there by the time the
      // block is.
      { rootMargin: "200px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, seen]);

  return seen;
}
