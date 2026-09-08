import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver and dockview builds its grid on one. The
// stub never fires: a jsdom element has no size to observe, and every
// assertion here is about what a panel renders, not how wide it is.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
