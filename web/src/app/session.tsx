import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { getMe, type Me } from "../api/client";

interface SessionState {
  me: Me | null; // null until /ui/api/me has answered once
  refresh: () => Promise<void>;
  requireLogin: () => void;
}

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);

  const refresh = useCallback(async () => {
    try {
      setMe(await getMe());
    } catch (err) {
      // A network failure or a non-JSON 5xx must still land the app in a
      // defined state instead of leaving `me` null ("Connecting…") forever.
      console.error("session refresh failed", err);
      setMe({ authenticated: false, expires_at: null, live_enabled: false });
    }
  }, []);

  // A 401 anywhere on the page flips the session to "not authenticated"
  // so the modal appears; the caller retries after login.
  const requireLogin = useCallback(() => {
    setMe({ authenticated: false, expires_at: null, live_enabled: false });
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(() => ({ me, refresh, requireLogin }), [me, refresh, requireLogin]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const value = useContext(SessionContext);
  if (value === null) throw new Error("useSession outside SessionProvider");
  return value;
}
