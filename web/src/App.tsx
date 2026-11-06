import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, clearSession, errorMessage, isUnauthorized, sessionToken } from "./api";
import { Brand, Icon } from "./design/Primitives";
import { Console } from "./Console";

type Session = "checking" | "locked" | "active" | "unavailable";

export function App() {
  const [session, setSession] = useState<Session>(() => sessionToken() ? "checking" : "locked");
  const [attempt, setAttempt] = useState(0);
  const [signOutError, setSignOutError] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const lock = useCallback(() => {
    if (!sessionToken()) setSession("locked");
  }, []);

  useEffect(() => {
    if (!sessionToken()) return;
    const controller = new AbortController();
    void api.session(controller.signal).then(() => {
      if (!controller.signal.aborted) setSession("active");
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setSession(isUnauthorized(error) ? "locked" : "unavailable");
    });
    return () => controller.abort();
  }, [attempt]);

  async function signOut() {
    const session = sessionToken();
    setSigningOut(true);
    setSignOutError("");
    try {
      await api.signOut();
    } catch (error) {
      if (!isUnauthorized(error)) setSignOutError("The service could not confirm session revocation.");
    } finally {
      if (sessionToken() === session) clearSession();
      lock();
      setSigningOut(false);
    }
  }

  if (session === "active") {
    return <Console onSignOut={signOut} onUnauthorized={lock} signingOut={signingOut} />;
  }

  return (
    <div className="lock-screen">
      <header className="lock-brand"><Brand /></header>
      <main id="main" className="lock-main">
        <div className="login-frame">
          <div className="login-icon"><Icon name="lock" /></div>
          <h1>Operator console</h1>
          {signOutError && <p role="alert">{signOutError}</p>}
          {session === "locked" ? <SignIn onSuccess={() => {
            setSignOutError("");
            setSession("active");
          }} /> : null}
          {session === "checking" ? <p role="status">Checking the local session…</p> : null}
          {session === "unavailable" && (
            <>
              <p role="alert">The local PAOS Core service is unavailable.</p>
              <button type="button" className="btn btn--primary" onClick={() => {
                setSession("checking");
                setAttempt((count) => count + 1);
              }}>Retry connection</button>
            </>
          )}
        </div>
        <p className="lock-foot">Local workspace · Synthetic release review</p>
      </main>
    </div>
  );
}

function SignIn({ onSuccess }: { onSuccess: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token.trim() || busy) return;
    setError("");
    setBusy(true);
    try {
      await api.signIn(token.trim());
      setToken("");
      onSuccess();
    } catch (caught) {
      setError(isUnauthorized(caught) ? "The token was rejected. Check the local startup output." : errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p>Enter the operator token printed when you started PAOS Core.</p>
      <form className="login-form" onSubmit={submit}>
        <label htmlFor="token">Operator token</label>
        <input id="token" name="token" type="password" autoComplete="off" required
          value={token} onChange={(event) => setToken(event.target.value)} />
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" className="btn btn--primary" disabled={busy || !token.trim()}>
          {busy ? "Signing in…" : "Sign in"} <Icon name="arrow" />
        </button>
      </form>
    </>
  );
}
