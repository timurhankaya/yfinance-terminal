// The one place dockview is touched.
//
// dockview owns the layout: which panels exist, how they are split, which
// one is active. What a panel *shows* rides in that panel's `params`, so
// there is one copy of it and `api.toJSON()` already carries it -- a
// second map of panel state beside the layout would drift apart on the
// first drag.
//
// Popouts are off: they rebuild the page's stylesheets as inline <style>
// in a second window and want a nonce for it, and `/ui`'s CSP issues
// none (`ui/pages.py`).
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { DockviewReact } from "dockview-react";
import type { DockviewApi, DockviewReadyEvent, IDockviewPanelProps } from "dockview-react";
import { getPanel } from "../commands/registry";
import type { Command, PanelArgs } from "../commands/types";
import { FrameProvider } from "../workspace/frame";

/** One panel's content: the command it is showing. */
export interface PanelParams {
  code: string;
  symbol: string | null;
  args: PanelArgs;
}

export interface PanelSeed extends PanelParams {
  id: string;
}

const COMPONENT = "panel";

/** What a panel does when it runs a command.
 *
 *  Carried by context rather than in `params`: it is not state, it must
 *  not reach `toJSON()`, and dockview re-renders a panel when its
 *  parameters change -- a function in there would re-render everything on
 *  every render of the page. */
const RunContext = createContext<(id: string, command: Command) => void>(() => undefined);

function Body({ code, symbol, args }: PanelParams) {
  const spec = getPanel(code);
  if (spec === undefined) return <p className="muted">Unknown function {code}.</p>;
  if (spec.needsSymbol && symbol === null) return <p className="muted">Type a symbol to begin.</p>;
  const Component = spec.component;
  return <Component symbol={symbol} args={args} />;
}

/** The single component dockview renders: a frame around a `PanelSpec`. */
function DockPanel(props: IDockviewPanelProps<PanelParams>) {
  const runInPanel = useContext(RunContext);
  const { api, params } = props;
  const [focused, setFocused] = useState(api.isActive);

  useEffect(() => {
    const handle = api.onDidActiveChange((event) => setFocused(event.isActive));
    return () => handle.dispose();
  }, [api]);

  const run = useCallback((command: Command) => runInPanel(api.id, command), [runInPanel, api.id]);

  return (
    <div className="dock-panel">
      <FrameProvider value={{ run, focused }}>
        <Body code={params.code} symbol={params.symbol} args={params.args} />
      </FrameProvider>
    </div>
  );
}

const components = { [COMPONENT]: DockPanel };

/** `AAPL GIP`, or just `HEAT` for a page that is not about one symbol. */
export function title(params: PanelParams): string {
  return params.symbol === null ? params.code : `${params.symbol} ${params.code}`;
}

export interface WorkspaceProps {
  /** The panels the page shows. The first render seeds the layout; after
   *  that dockview owns it and these updates are content. */
  panels: PanelSeed[];
  /** A panel ran a command. The page decides what that means: an address
   *  change where the address is the page, a content swap where it is not. */
  onRun: (id: string, command: Command) => void;
  /** The panel the keyboard is talking to changed. The shell needs this
   *  because the command box lives outside the dock. */
  onActive?: (id: string | null) => void;
}

export function Workspace({ panels, onRun, onActive }: WorkspaceProps) {
  const apiRef = useRef<DockviewApi | null>(null);
  const seedRef = useRef(panels);
  seedRef.current = panels;
  const runRef = useRef(onRun);
  runRef.current = onRun;
  const activeRef = useRef(onActive);
  activeRef.current = onActive;

  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    event.api.onDidActivePanelChange((change) => activeRef.current?.(change.panel?.id ?? null));
    let previous: string | undefined;
    for (const seed of seedRef.current) {
      event.api.addPanel<PanelParams>({
        id: seed.id,
        component: COMPONENT,
        title: title(seed),
        params: { code: seed.code, symbol: seed.symbol, args: seed.args },
        position: previous === undefined ? undefined : { referencePanel: previous, direction: "right" },
      });
      previous = seed.id;
    }
  }, []);

  // Content changes reach dockview through each panel's own parameters,
  // which is where the layout already keeps them.
  useEffect(() => {
    const api = apiRef.current;
    if (api === null) return;
    // A page can hand over a different set of panels than it had -- going
    // from an address, which is one panel, to a saved page, which is its
    // own. The ones it no longer names are gone, not hidden.
    const wanted = new Set(panels.map((seed) => seed.id));
    for (const panel of api.panels) {
      if (!wanted.has(panel.id)) api.removePanel(panel);
    }
    for (const seed of panels) {
      const params = { code: seed.code, symbol: seed.symbol, args: seed.args };
      const panel = api.getPanel(seed.id);
      if (panel === undefined) {
        api.addPanel<PanelParams>({
          id: seed.id,
          component: COMPONENT,
          title: title(seed),
          params,
          position: { direction: "right" },
        });
        continue;
      }
      panel.api.updateParameters(params);
      panel.api.setTitle(title(seed));
    }
  }, [panels]);

  const run = useCallback((id: string, command: Command) => runRef.current(id, command), []);

  return (
    <RunContext.Provider value={run}>
      <DockviewReact className="dock" components={components} onReady={onReady} disableFloatingGroups />
    </RunContext.Provider>
  );
}
