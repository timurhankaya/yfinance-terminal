// Which panel is to the left of this one. dockview has no directional
// navigation, so the answer is geometry over real rectangles, testable
// here; the dock only hands over what it measured.

/** The four ways a reader can mean "the next one over". */
export enum Direction {
  Left = "left",
  Right = "right",
  Up = "up",
  Down = "down",
}

export interface Box {
  id: string;
  left: number;
  top: number;
  width: number;
  height: number;
}

interface Centre {
  id: string;
  x: number;
  y: number;
}

function centre(box: Box): Centre {
  return { id: box.id, x: box.left + box.width / 2, y: box.top + box.height / 2 };
}

/** The panel next to `activeId` in `direction`, or null at the edge of
 *  the page. Nearest along the direction wins; a tie goes to whichever
 *  strays least across it, not to whichever dockview created first. */
export function pickNeighbour(boxes: Box[], activeId: string, direction: Direction): string | null {
  const active = boxes.find((box) => box.id === activeId);
  if (active === undefined) return null;
  const from = centre(active);

  let best: { id: string; along: number; across: number } | null = null;
  for (const box of boxes) {
    if (box.id === activeId) continue;
    const to = centre(box);
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const horizontal = direction === Direction.Left || direction === Direction.Right;
    const along = horizontal ? dx : dy;
    const across = Math.abs(horizontal ? dy : dx);
    const wanted = direction === Direction.Right || direction === Direction.Down ? along > 0 : along < 0;
    // A panel that is not in that direction at all is not a candidate,
    // and neither is one that is more sideways than onward: from a left
    // pane, the panel directly below is "down", not "right".
    if (!wanted || Math.abs(along) <= across) continue;
    const distance = Math.abs(along);
    if (best === null || distance < best.along || (distance === best.along && across < best.across)) {
      best = { id: box.id, along: distance, across };
    }
  }
  return best === null ? null : best.id;
}
