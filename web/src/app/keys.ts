import { useEffect } from "react";
import type { RefObject } from "react";
import { useNavigate } from "react-router";
import { PAGE_KEYS } from "../workspace/store";
import { Direction } from "../workspace/neighbour";

export interface GlobalKeysArgs {
  inputRef: RefObject<HTMLInputElement | null>;
  paletteOpen: boolean;
  openHelp: () => void;
  /** F1-F4 and F7-F10: the saved page in that position. */
  onPageKey?: (key: string) => void;
  /** Ctrl+Shift+arrow: hand the keyboard to the panel that way. */
  onMoveFocus?: (direction: Direction) => void;
}

//: Ctrl+Shift rather than Alt: Alt+left and Alt+right are back and
//: forward on Windows and Linux, and this terminal already spends Esc
//: and Shift+Esc on those.
const ARROWS: Record<string, Direction> = {
  ArrowLeft: Direction.Left,
  ArrowRight: Direction.Right,
  ArrowUp: Direction.Up,
  ArrowDown: Direction.Down,
};

function isTextInput(el: Element | null): boolean {
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement;
}

/** Global shortcuts for the shell.
 *
 * Inert entirely while the palette is open (it owns the keyboard then).
 * Escape when the command box is focused is left to the box's own
 * keydown handler (it clears the draft and blurs); this listener only
 * turns Escape into "go back" when focus is elsewhere. Shift+Esc always
 * goes forward. The remaining shortcuts (Ctrl/Meta+K, "/", "?") are inert
 * while any INPUT/TEXTAREA is focused, so they never interrupt typing. */
export function useGlobalKeys({
  inputRef,
  paletteOpen,
  openHelp,
  onPageKey,
  onMoveFocus,
}: GlobalKeysArgs): void {
  const navigate = useNavigate();

  useEffect(() => {
    function handler(event: KeyboardEvent) {
      if (paletteOpen) return;
      const onCommandBox = document.activeElement === inputRef.current;

      // Moving between panels works from the command box too: a reader
      // who has just typed into it should not have to click a panel to
      // point the keyboard somewhere else.
      const arrow = (event.ctrlKey || event.metaKey) && event.shiftKey ? ARROWS[event.key] : undefined;
      if (arrow !== undefined) {
        event.preventDefault();
        onMoveFocus?.(arrow);
        return;
      }

      if (event.key === "Escape") {
        // Shift+Esc is "forward" from anywhere, the command box included
        // (the box only claims a plain Escape).
        if (event.shiftKey) {
          void navigate(1);
          return;
        }
        if (onCommandBox) return; // the command box handles its own Escape
        // Any other text field (the palette's input) keeps its Escape:
        // navigating history from behind a modal would move the page the
        // user cannot see.
        if (isTextInput(document.activeElement)) return;
        void navigate(-1);
        return;
      }

      // The page keys are the one exception to "shortcuts are inert while
      // a text field has focus": a function key types nothing, and a
      // reader half-way through a command still expects F2 to be their
      // second page.
      if (PAGE_KEYS.includes(event.key)) {
        event.preventDefault();
        onPageKey?.(event.key);
        return;
      }

      if (isTextInput(document.activeElement)) return;

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        inputRef.current?.focus();
        return;
      }
      if (event.key === "/") {
        event.preventDefault();
        inputRef.current?.focus();
        return;
      }
      if (event.key === "?") {
        openHelp();
      }
    }

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [inputRef, paletteOpen, openHelp, onPageKey, onMoveFocus, navigate]);
}
