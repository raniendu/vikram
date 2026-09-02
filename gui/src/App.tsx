import { useEffect, useState } from "react";
import { AgentSummary, SidecarError, connect, listSessions } from "./api/client";
import { ThemeToggle } from "./components/primitives";
import { AgentEditor } from "./screens/AgentEditor";
import { AgentList } from "./screens/AgentList";
import { Chat } from "./screens/Chat";
import { Doctor } from "./screens/Doctor";
import { Playground } from "./screens/Playground";
import { Settings } from "./screens/Settings";

type Tab = "agents" | "sessions" | "playground" | "settings" | "doctor";

const TABS: [Tab, string][] = [
  ["agents", "Agents"],
  ["sessions", "Sessions"],
  ["playground", "Playground"],
  ["settings", "Settings"],
  ["doctor", "Doctor"],
];

const THEME_KEY = "vikram.theme";

export function App() {
  const [ready, setReady] = useState(false);
  const [failure, setFailure] = useState<SidecarError | null>(null);
  const [tab, setTab] = useState<Tab>("agents");
  const [chatting, setChatting] = useState<AgentSummary | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(0);
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

  // The masthead carries the count so a session parked on an approval is
  // visible from any screen — the 300s timeout runs whether you are looking
  // at that session or not.
  useEffect(() => {
    if (!ready) return;
    let alive = true;
    const poll = () =>
      listSessions()
        .then((p) => alive && setWaiting(p.waiting))
        .catch(() => {});
    poll();
    const id = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [ready]);

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

  const masthead = (
    <header className="masthead">
      <div className="wordmark">Vikram Studio</div>
      <nav className="tabs">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            className={tab === key && !chatting && !editing ? "active" : ""}
            onClick={() => {
              setChatting(null);
              setEditing(null);
              setTab(key);
            }}
          >
            {label}
            {key === "sessions" && waiting > 0 && (
              <span className="tab-count">{waiting}</span>
            )}
          </button>
        ))}
      </nav>
      <ThemeToggle dark={dark} onToggle={() => setDark(!dark)} />
    </header>
  );

  // Opening a session from the agent list, with a folder still to pick.
  if (chatting) {
    return (
      <main className="app">
        {masthead}
        <Chat agent={chatting} onBack={() => setChatting(null)} />
      </main>
    );
  }

  if (editing) {
    return (
      <main className="app">
        {masthead}
        <AgentEditor agentId={editing} onBack={() => setEditing(null)} />
      </main>
    );
  }

  return (
    <main className="app">
      {masthead}
      {tab === "agents" && <AgentList onChat={setChatting} onEdit={setEditing} />}
      {tab === "sessions" && <Chat agent={null} onBack={() => setTab("agents")} />}
      {tab === "playground" && <Playground />}
      {tab === "settings" && <Settings />}
      {tab === "doctor" && <Doctor />}
    </main>
  );
}
