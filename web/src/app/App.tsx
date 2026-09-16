import { Navigate, Route, Routes } from "react-router";
import { HOME_PATH } from "../commands/parser";
import { Shell } from "./Shell";
import { dockviewEnabled } from "./config";

export function AppRoutes() {
  return (
    <Routes>
      {/* Two shapes: a market page has no symbol in its address, a
          symbol's detail does. `Shell` tells them apart by whether the
          `symbol` param is present. `/ui` is the home, not a redirect. */}
      <Route path="/ui" element={<Shell />} />
      <Route path="/ui/m/:code" element={<Shell />} />
      <Route path="/ui/t/:symbol" element={<Shell />} />
      <Route path="/ui/t/:symbol/:code" element={<Shell />} />
      {/* A saved page is neither: its address names the page, and what
          it holds is the layout, not one command. */}
      <Route path="/ui/w/:name" element={dockviewEnabled() ? <Shell /> : <Navigate to={HOME_PATH} replace />} />
      <Route path="*" element={<Navigate to={HOME_PATH} replace />} />
    </Routes>
  );
}
