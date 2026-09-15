import "@testing-library/jest-dom/vitest";
import { JSDOM } from "jsdom";

// Node 22 exposes a global `localStorage` accessor that returns undefined
// unless it is started with `--localstorage-file`. Vitest's jsdom window is
// the global object, so that accessor shadows jsdom's origin-backed storage.
// Borrow a real storage area from a same-origin jsdom instance for tests.
const storageWindow = new JSDOM("", { url: "http://localhost/" }).window;
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: storageWindow.localStorage,
});
Object.defineProperty(globalThis, "Storage", {
  configurable: true,
  value: storageWindow.Storage,
});

// jsdom has no ResizeObserver and dockview builds its grid on one. The
// stub never fires: a jsdom element has no size to observe, and every
// assertion here is about what a panel renders, not how wide it is.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

// jsdom implements no scrolling at all, so `scrollIntoView` is simply
// absent from the prototype -- and `SymbolBand` calls it on mount to
// bring the active function into view. A no-op is the honest stub: there
// is no scrollport in jsdom for the call to have meant anything.
Element.prototype.scrollIntoView ??= function scrollIntoView(): void {};
