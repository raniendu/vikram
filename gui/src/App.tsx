import { useCallback, useEffect, useState } from "react";
import {
  AgentSummary,
  SessionInfo,
  SidecarError,
  connect,
  listSessions,
} from "./api/client";
import { Rail, View } from "./components/Rail";
import { AgentEditor } from "./screens/AgentEditor";
import { AgentList } from "./screens/AgentList";
import { Chat } from "./screens/Chat";
import { Doctor } from "./screens/Doctor";
import { NewSession } from "./screens/NewSession";
import { Playground } from "./screens/Playground";
import { Settings } from "./screens/Settings";

const THEME_KEY = "vikram.theme";

/** What fills the pane beside the rail. */
type Pane =
  | { kind: "view"; view: View }
  | { kind: "session"; sessionId: string }
  | { kind: "new"; agent: AgentSummary | null }
  | { kind: "edit"; agentId: string };

export function App() {
  const [ready, setReady] = useState(false);
  const [failure, setFailure] = useState<SidecarError | null>(null);
  const [pane, setPane] = useState<Pane>({ kind: "view", view: "agents" });
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [dark, setDark] = useState(false);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(THEME_KEY);
      if (stored) setDark(stored === "dark");
    } catch {
      /* private window, or site data blocked */
    }
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    try {
      localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
    } catch {
      /* nothing to do; the choice just will not persist */
    }
  }, [dark]);

  useEffect(() => {
    connect()
      .then(() => setReady(true))
      .catch((e) => setFailure(e as SidecarError));
  }, []);

  // Sessions live here rather than in Chat: the rail shows them on every
  // screen, so no single screen can own them.
  const refreshSessions = useCallback(async () => {
    try {
      const live = (await listSessions()).sessions;
      setSessions(live);
      return live;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    if (!ready) return;
    refreshSessions();
    const id = setInterval(refreshSessions, 3000);
    return () => clearInterval(id);
  }, [ready, refreshSessions]);

  if (failure) {
    return (
      <main className="boot">
        <h1>Can’t start Vikram</h1>
        <p className="error">{failure.message ?? String(failure)}</p>
        {failure.hint && <p className="muted">{failure.hint}</p>}
        <p className="muted small">
          Launching from a terminal with <span className="mono">vikram gui</span>{" "}
          passes the right path through.
        </p>
      </main>
    );
  }

  if (!ready) {
    return (
      <main className="boot">
        <h1>Starting Vikram…</h1>
        <p className="muted">Loading the agent runtime.</p>
      </main>
    );
  }

  return (
    <main className="app">
      <Rail
        view={pane.kind === "view" ? pane.view : null}
        sessions={sessions}
        currentSession={pane.kind === "session" ? pane.sessionId : null}
        dark={dark}
        onView={(view) => setPane({ kind: "view", view })}
        onSession={(sessionId) => setPane({ kind: "session", sessionId })}
        onNewSession={() => setPane({ kind: "new", agent: null })}
        onToggleTheme={() => setDark(!dark)}
      />

      {pane.kind === "view" && pane.view === "agents" && (
        <AgentList
          onChat={(agent) => setPane({ kind: "new", agent })}
          onEdit={(agentId) => setPane({ kind: "edit", agentId })}
        />
      )}
      {pane.kind === "view" && pane.view === "playground" && <Playground />}
      {pane.kind === "view" && pane.view === "settings" && <Settings />}
      {pane.kind === "view" && pane.view === "doctor" && <Doctor />}

      {pane.kind === "session" && (
        <Chat
          key={pane.sessionId}
          sessionId={pane.sessionId}
          session={sessions.find((s) => s.session_id === pane.sessionId) ?? null}
          onEnded={() => {
            refreshSessions();
            setPane({ kind: "view", view: "agents" });
          }}
        />
      )}

      {pane.kind === "new" && (
        <NewSession
          agent={pane.agent}
          onCancel={() => setPane({ kind: "view", view: "agents" })}
          onStarted={async (sessionId) => {
            await refreshSessions();
            setPane({ kind: "session", sessionId });
          }}
        />
      )}

      {pane.kind === "edit" && (
        <AgentEditor agentId={pane.agentId} />
      )}
    </main>
  );
}
