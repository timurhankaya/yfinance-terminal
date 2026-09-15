/** Public runtime settings embedded by the API in the HTML shell. */
export function dockviewEnabled(): boolean {
  return document.querySelector<HTMLMetaElement>('meta[name="yfin-dockview-enabled"]')?.content !== "false";
}
