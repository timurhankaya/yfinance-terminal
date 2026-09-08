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
import { Layout } from "../commands/types";
import type { Command, PanelArgs } from "../commands/types";
import { FrameProvider } from "../workspace/frame";
import { groupLabel, symbolFor } from "../workspace/groups";
import type { Group, GroupSymbols } from "../workspace/groups";
import { Strip } from "./Strip";

/** One panel's content: the command it is showing, and the letter whose
 *  symbol it follows -- null when it is about its own. */
export interface PanelParams {
  code: string;
  symbol: string | null;
  args: PanelArgs;
  group: Group | null;
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

/** What each letter is pointed at. In a context rather than in `params`
 *  because it belongs to the page, not to any one panel: a letter's
 *  symbol is the same fact for every panel wearing it. */
const GroupContext = createContext<GroupSymbols>({});

function Body({ code, symbol, args, group }: PanelParams) {
  const groups = useContext(GroupContext);
  const spec = getPanel(code);
  const shown = symbolFor(groups, group, symbol);
  if (spec === undefined) return <p className="muted">Unknown function {code}.</p>;
  if (spec.needsSymbol && shown === null) return <p className="muted">Type a symbol to begin.</p>;
  const Component = spec.component;
  return (
    <>
      {/* The strip belongs to the panel, not to the page: two `headed`
          panels in two groups are two symbols, and one band above the
          dock could only have told the truth about one of them. */}
      {spec.layout === Layout.Headed && shown !== null && <Strip symbol={shown} live />}
      <Component symbol={shown} args={args} />
    </>
  );
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

  const edge = params.group === null ? "dock-panel" : `dock-panel group-${params.group}`;
  return (
    <div className={edge}>
      <FrameProvider value={{ run, focused }}>
        <Body {...params} />
      </FrameProvider>
    </div>
  );
}

const components = { [COMPONENT]: DockPanel };

/** `AAPL GIP`, `A · AAPL GIP` once it follows a letter, or just `HEAT`
 *  for a page that is not about one symbol. */
export function title(params: PanelParams, groups: GroupSymbols = {}): string {
  const shown = symbolFor(groups, params.group, params.symbol);
  const body = shown === null ? params.code : `${shown} ${params.code}`;
  return params.group === null ? body : `${groupLabel(params.group)} · ${body}`;
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
  /** What each letter is pointed at. */
  groups?: GroupSymbols;
}

export function Workspace({ panels, onRun, onActive, groups = {} }: WorkspaceProps) {
  const apiRef = useRef<DockviewApi | null>(null);
  const seedRef = useRef(panels);
  seedRef.current = panels;
  const runRef = useRef(onRun);
  runRef.current = onRun;
  const activeRef = useRef(onActive);
  activeRef.current = onActive;
  const groupsRef = useRef(groups);
  groupsRef.current = groups;

  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    event.api.onDidActivePanelChange((change) => activeRef.current?.(change.panel?.id ?? null));
    let previous: string | undefined;
    for (const seed of seedRef.current) {
      event.api.addPanel<PanelParams>({
        id: seed.id,
        component: COMPONENT,
        title: title(seed, groupsRef.current),
        params: { code: seed.code, symbol: seed.symbol, args: seed.args, group: seed.group },
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
      const params = { code: seed.code, symbol: seed.symbol, args: seed.args, group: seed.group };
      const panel = api.getPanel(seed.id);
      if (panel === undefined) {
        api.addPanel<PanelParams>({
          id: seed.id,
          component: COMPONENT,
          title: title(seed, groupsRef.current),
          params,
          position: { direction: "right" },
        });
        continue;
      }
      panel.api.updateParameters(params);
      panel.api.setTitle(title(seed, groups));
    }
  }, [panels, groups]);

  const run = useCallback((id: string, command: Command) => runRef.current(id, command), []);

  return (
    <GroupContext.Provider value={groups}>
      <RunContext.Provider value={run}>
        <DockviewReact className="dock" components={components} onReady={onReady} disableFloatingGroups />
      </RunContext.Provider>
    </GroupContext.Provider>
  );
}
