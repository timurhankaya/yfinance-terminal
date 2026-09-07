import { useEffect } from "react";
import type { RefObject } from "react";
import { useNavigate } from "react-router";

export interface GlobalKeysArgs {
  inputRef: RefObject<HTMLInputElement | null>;
  paletteOpen: boolean;
  openHelp: () => void;
}

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
export function useGlobalKeys({ inputRef, paletteOpen, openHelp }: GlobalKeysArgs): void {
  const navigate = useNavigate();

  useEffect(() => {
    function handler(event: KeyboardEvent) {
      if (paletteOpen) return;
      const onCommandBox = document.activeElement === inputRef.current;

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
  }, [inputRef, paletteOpen, openHelp, navigate]);
}
