import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, login } from "../api/client";
import { useSession } from "./session";

export function LoginModal() {
  const { refresh } = useSession();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(draft);
      setDraft("");
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) setError("Too many attempts. Wait a minute.");
      else setError("Wrong password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="login">
      <form className="modal" onSubmit={submit}>
        <h1>yfin terminal</h1>
        <label>
          Password
          <input
            aria-label="password"
            type="password"
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={busy}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy || draft === ""}>Sign in</button>
      </form>
    </div>
  );
}
