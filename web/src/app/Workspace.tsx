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
import type {
  DockviewApi,
  DockviewReadyEvent,
  IDockviewHeaderActionsProps,
  IDockviewPanelProps,
  SerializedDockview,
} from "dockview-react";
import { getPanel, retiredNote } from "../commands/registry";
import { Layout } from "../commands/types";
import type { Command, PanelArgs } from "../commands/types";
import { FrameProvider } from "../workspace/frame";
import { groupLabel, symbolFor } from "../workspace/groups";
import type { Group, GroupSymbols } from "../workspace/groups";
import { SymbolBand } from "./SymbolBand";

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

/** What the tab bar's + does. In a context for the same reason the run
 *  callback is: dockview renders the header actions in its own tree, and
 *  this must not be serialised with a panel. */
const SplitContext = createContext<(command: Command) => void>(() => undefined);

function Body({ code, symbol, args, group }: PanelParams) {
  const groups = useContext(GroupContext);
  const spec = getPanel(code);
  const shown = symbolFor(groups, group, symbol);
  if (spec === undefined) {
    // A saved page or a shared link can carry a code this build no
    // longer has; say where its work went rather than only that it is
    // gone.
    const moved = retiredNote(code);
    return (
      <p className="muted">
        {code} is no longer a function{moved === undefined ? "." : `. Use ${moved}.`}
      </p>
    );
  }
  if (spec.needsSymbol && shown === null) return <p className="muted">Type a symbol to begin.</p>;
  const Component = spec.component;
  return (
    <>
      {/* The band belongs to the panel, not to the page: two symbol
          panels in two groups are two symbols, and one band above the
          dock could only have told the truth about one of them.
          `needsSymbol` decides whether there is a band; `Layout.Headed`
          decides whether the price inside it is live. */}
      {spec.needsSymbol && shown !== null && (
        <SymbolBand symbol={shown} code={code} live={spec.layout === Layout.Headed} />
      )}
      {/* The padding is the BODY's, not the panel's. It used to be on
          `.dock-panel`, and the band undid it with a negative margin to
          reach the panel's edge -- which `position: sticky` then cancelled,
          because sticky refuses to place an element above its scrollport's
          top and left a ten-pixel gap over the band on every panel. */}
      <div className="panel-body">
        <Component symbol={shown} args={args} />
      </div>
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

/** The one thing on screen that says a page can hold more than one panel.
 *
 *  It copies the panel it sits on -- same function, same symbol, beside
 *  it, which is the split a terminal reader wants most (one chart, two
 *  symbols) and needs no empty state to design. It goes through the
 *  shell rather than adding a panel here: on a page that is an address,
 *  splitting is what MOVES the reader to a layout, and only the shell
 *  knows that. The title carries the keyboard way, so the button teaches
 *  the shortcut. */
function AddPanel(props: IDockviewHeaderActionsProps) {
  const split = useContext(SplitContext);
  const active = props.group.activePanel;
  if (active === undefined) return null;
  const params = active.params as PanelParams;
  return (
    <button
      type="button"
      className="dock-add"
      title="Open a second panel beside this one (Ctrl+Enter)"
      aria-label="Open a second panel"
      onClick={() => split({ symbol: params.symbol, code: params.code, args: params.args })}
    >
      +
    </button>
  );
}

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
  /** A layout to restore instead of seeding one from `panels`. Read once,
   *  on the first render: after that dockview owns the arrangement. */
  initial?: SerializedDockview;
  /** The arrangement changed -- a drag, a split, a close. Fired with
   *  dockview's own document, which is the only thing that knows where
   *  the panels are. Absent on a page that IS an address: there is
   *  nothing to save, because the address already says it. */
  onLayout?: (dock: SerializedDockview) => void;
  /** `initial` would not load. The page it came from is dropped by the
   *  caller; the layout falls back to seeding from `panels`. */
  onLayoutError?: () => void;
  /** The dock, once. The shell needs it for the one thing it cannot ask
   *  for declaratively: where the panels are on screen, which is what
   *  "the panel to the left" means. */
  onApi?: (api: DockviewApi) => void;
  /** The tab bar's + was pressed on a panel showing this command. */
  onSplit?: (command: Command) => void;
}

export function Workspace(props: WorkspaceProps) {
  const { panels, onRun, onActive, groups = {}, initial, onLayout, onLayoutError, onSplit } = props;
  const apiRef = useRef<DockviewApi | null>(null);
  const seedRef = useRef(panels);
  seedRef.current = panels;
  const runRef = useRef(onRun);
  runRef.current = onRun;
  const activeRef = useRef(onActive);
  activeRef.current = onActive;
  const groupsRef = useRef(groups);
  groupsRef.current = groups;
  const apiOut = useRef(props.onApi);
  apiOut.current = props.onApi;

  // Refs, not dependencies: `onReady` runs once, and rebuilding the dock
  // because a callback changed identity would throw away the layout.
  const initialRef = useRef(initial);
  initialRef.current = initial;
  const layoutRef = useRef(onLayout);
  layoutRef.current = onLayout;
  const layoutErrorRef = useRef(onLayoutError);
  layoutErrorRef.current = onLayoutError;

  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    apiOut.current?.(event.api);
    event.api.onDidActivePanelChange((change) => activeRef.current?.(change.panel?.id ?? null));
    // A saved layout is restored whole -- sizes, splits and the active
    // panel -- because those are exactly what an address could not carry.
    // Only dockview can say whether a stored document loads, so this is
    // where a page-level failure is caught (spec, "Kararlar" 9).
    const stored = initialRef.current;
    if (stored !== undefined) {
      try {
        event.api.fromJSON(stored);
        event.api.onDidLayoutChange(() => layoutRef.current?.(event.api.toJSON()));
        return;
      } catch {
        event.api.clear();
        layoutErrorRef.current?.();
      }
    }
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
    // Subscribed after the seeding, so the page is not written back
    // once per panel while it is being built.
    event.api.onDidLayoutChange(() => layoutRef.current?.(event.api.toJSON()));
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
  const splitRef = useRef(onSplit);
  splitRef.current = onSplit;
  const split = useCallback((command: Command) => splitRef.current?.(command), []);

  return (
    <GroupContext.Provider value={groups}>
      <SplitContext.Provider value={split}>
        <RunContext.Provider value={run}>
        <DockviewReact
          className="dock"
          components={components}
          onReady={onReady}
          rightHeaderActionsComponent={AddPanel}
          disableFloatingGroups
        />
        </RunContext.Provider>
      </SplitContext.Provider>
    </GroupContext.Provider>
  );
}
