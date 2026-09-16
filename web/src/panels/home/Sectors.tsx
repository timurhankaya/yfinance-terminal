// The sector map, at home.
//
// The same read and the same treemap `HEAT` draws: `loadSectors` is
// exported from there rather than copied here.
import { HeatPeriod, loadSectors } from "../HEAT";
import type { TreemapItem } from "../viz";
import { Treemap } from "../viz";
import { LoadState, usePanelData } from "../common";
import { Block, Failed, Waiting } from "./Block";

export function Sectors({ onOpen }: { onOpen: (key: string) => void }) {
  return (
    <Block title="Sectors" onOpen={() => onOpen("")}>
      {(visible) => (visible ? <SectorsBody onOpen={onOpen} /> : <Waiting what="sectors" />)}
    </Block>
  );
}

function SectorsBody({ onOpen }: { onOpen: (key: string) => void }) {
  const { state } = usePanelData<TreemapItem[]>(
    "home-sectors",
    () => loadSectors(HeatPeriod.Day),
    (cells) => cells.length === 0,
  );
  if (state.kind === LoadState.Loading) return <Waiting what="sectors" />;
  if (state.kind === LoadState.Error) return <Failed what="Sectors" message={state.message} />;
  if (state.kind === LoadState.Empty) return <p className="muted">No sector metrics yet.</p>;
  if (state.kind !== LoadState.Ready) return null;

  return (
    <Treemap
      items={state.data}
      label="Sectors by market value, coloured by today's move"
      span={3}
      onOpen={onOpen}
      format={(item) => item.percent === null ? "No move data" : `${item.percent > 0 ? "+" : ""}${item.percent.toFixed(2)}%`}
    />
  );
}
