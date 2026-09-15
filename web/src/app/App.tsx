import { Navigate, Route, Routes } from "react-router";
import { HOME_PATH } from "../commands/parser";
import { Shell } from "./Shell";
import { dockviewEnabled } from "./config";

export function AppRoutes() {
  return (
    <Routes>
      {/* Two shapes, because there are two kinds of page. A market page
          has no symbol in its address -- a screener is not a property of
          a symbol -- and a symbol's detail does. `Shell` tells them apart
          by whether the `symbol` param is present.

          `/ui` is the home rather than a redirect. It used to send a
          returning reader to whatever `localStorage` said they last
          looked at, which was the terminal's only piece of state outside
          the URL; with a real landing page that redirect has nothing
          left to do, and the storage went with it. */}
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
