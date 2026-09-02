import { useEffect, useState } from "react";
import { AgentSummary, SidecarError, connect } from "./api/client";
import { AgentList } from "./screens/AgentList";
import { Chat } from "./screens/Chat";
import { Doctor } from "./screens/Doctor";

type Tab = "agents" | "doctor";

export function App() {
  const [ready, setReady] = useState(false);
  const [failure, setFailure] = useState<SidecarError | null>(null);
  const [tab, setTab] = useState<Tab>("agents");
  const [chatting, setChatting] = useState<AgentSummary | null>(null);

  useEffect(() => {
    connect()
      .then(() => setReady(true))
      .catch((e) => setFailure(e as SidecarError));
  }, []);

  // A named failure beats a spinner that never resolves — the usual cause is
  // a Finder launch, which inherits no login PATH and so cannot see
  // ~/.local/bin/vikram-api.
  if (failure) {
    return (
      <main className="boot">
        <h1>Can't start Vikram</h1>
        <p className="error">{failure.message ?? String(failure)}</p>
        {failure.hint && <p className="muted">{failure.hint}</p>}
        <p className="muted small">
          Launching from a terminal with <code>vikram gui</code> passes the
          right path through.
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

  if (chatting) {
    return (
      <main className="app">
        <Chat agent={chatting} onBack={() => setChatting(null)} />
      </main>
    );
  }

  return (
    <main className="app">
      <nav className="tabs">
        <button
          className={tab === "agents" ? "active" : ""}
          onClick={() => setTab("agents")}
        >
          Agents
        </button>
        <button
          className={tab === "doctor" ? "active" : ""}
          onClick={() => setTab("doctor")}
        >
          Doctor
        </button>
      </nav>
      {tab === "agents" ? <AgentList onChat={setChatting} /> : <Doctor />}
    </main>
  );
}
