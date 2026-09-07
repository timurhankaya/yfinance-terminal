import { Navigate, Route, Routes } from "react-router";
import { LAST_KEY, Shell } from "./Shell";

function RootRedirect() {
  let last: string | null = null;
  try {
    last = localStorage.getItem(LAST_KEY);
  } catch {
    last = null;
  }
  return <Navigate to={last && last.startsWith("/ui/t/") ? last : "/ui/t/-/DES"} replace />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/ui" element={<RootRedirect />} />
      <Route path="/ui/t/:symbol/:code" element={<Shell />} />
      <Route path="*" element={<Navigate to="/ui" replace />} />
    </Routes>
  );
}
