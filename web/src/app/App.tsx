import { Navigate, Route, Routes } from "react-router";
import { LoginModal } from "./LoginModal";
import { LAST_KEY, Shell } from "./Shell";
import { SessionProvider, useSession } from "./session";

function RootRedirect() {
  let last: string | null = null;
  try {
    last = localStorage.getItem(LAST_KEY);
  } catch {
    last = null;
  }
  return <Navigate to={last && last.startsWith("/ui/t/") ? last : "/ui/t/-/DES"} replace />;
}

function Gate() {
  const { me } = useSession();
  if (me === null) return <p className="muted">Connecting…</p>;
  return (
    <>
      <Routes>
        <Route path="/ui" element={<RootRedirect />} />
        <Route path="/ui/t/:symbol/:code" element={<Shell />} />
        <Route path="*" element={<Navigate to="/ui" replace />} />
      </Routes>
      {!me.authenticated && <LoginModal />}
    </>
  );
}

export function AppRoutes() {
  return (
    <SessionProvider>
      <Gate />
    </SessionProvider>
  );
}
