import type { PanelSpec } from "./types";

const panels = new Map<string, PanelSpec>();

export function registerPanel(spec: PanelSpec): void {
  panels.set(spec.code.toUpperCase(), spec);
}

export function getPanel(code: string): PanelSpec | undefined {
  return panels.get(code.toUpperCase());
}

export function listPanels(): PanelSpec[] {
  return [...panels.values()].sort((a, b) => a.code.localeCompare(b.code));
}

export function isMnemonic(token: string): boolean {
  return panels.has(token.toUpperCase());
}

/** Tests register their own minimal panels. */
export function clearRegistry(): void {
  panels.clear();
}
