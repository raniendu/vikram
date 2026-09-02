import { SessionInfo } from "../api/client";
import { Dot, Eyebrow } from "./primitives";

/** The whole navigation: there is no top bar.
 *
 * One column holds where you are and what is running, so the content pane
 * starts at the top of the window. Rows are 26–27px, one line each — the
 * activity text a session is doing lives in its own header instead.
 */

export type View = "agents" | "playground" | "settings" | "doctor";

const NAV: [View, string][] = [
  ["agents", "Agents"],
  ["playground", "Playground"],
  ["settings", "Settings"],
  ["doctor", "Doctor"],
];

interface Props {
  view: View | null;
  sessions: SessionInfo[];
  currentSession: string | null;
  dark: boolean;
  onView: (view: View) => void;
  onSession: (sessionId: string) => void;
  onNewSession: () => void;
  onToggleTheme: () => void;
}

export function Rail({
  view,
  sessions,
  currentSession,
  dark,
  onView,
  onSession,
  onNewSession,
  onToggleTheme,
}: Props) {
  const waiting = sessions.filter((s) => s.state === "needs").length;

  return (
    <aside className="rail">
      <div className="rail-top">
        <div className="wordmark">Vikram</div>
        <button
          className="rail-icon"
          onClick={onToggleTheme}
          title={dark ? "Switch to light" : "Switch to dark"}
          aria-label="Toggle theme"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="1.7">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none" />
          </svg>
        </button>
      </div>

      <nav className="rail-nav">
        {NAV.map(([key, label]) => (
          <button
            key={key}
            className={view === key ? "active" : ""}
            onClick={() => onView(key)}
          >
            <span className="mark" />
            <span className="grow">{label}</span>
          </button>
        ))}
      </nav>

      <div className="rail-sessions">
        <div className="rail-head">
          <Eyebrow className="grow">Sessions</Eyebrow>
          {/* The approval timeout runs whether or not you are looking at that
              session, so the count is visible from every screen. */}
          <Eyebrow className={waiting ? "accent" : undefined}>
            {waiting ? `${waiting} waiting` : String(sessions.length)}
          </Eyebrow>
        </div>

        {groupByAgent(sessions).map(([agentName, items]) => (
          <div key={agentName}>
            <Eyebrow className="rail-group">
              {agentName} {items.length}
            </Eyebrow>
            {items.map((s) => (
              <button
                key={s.session_id}
                className={`rail-item${s.session_id === currentSession ? " active" : ""}`}
                onClick={() => onSession(s.session_id)}
                title={`${s.workspace} — ${s.activity || "idle"}`}
              >
                <Dot live={s.state !== "idle"} />
                <span className="name">{basename(s.workspace)}</span>
                {s.state === "needs" && (
                  <span className="eyebrow accent" style={{ fontSize: 9 }}>!</span>
                )}
              </button>
            ))}
          </div>
        ))}

        {sessions.length === 0 && (
          <p className="muted small" style={{ padding: "8px 16px" }}>
            None running.
          </p>
        )}
      </div>

      <div className="rail-foot">
        <button className="eyebrow" onClick={onNewSession}>
          + New session
        </button>
      </div>
    </aside>
  );
}

function groupByAgent(sessions: SessionInfo[]): [string, SessionInfo[]][] {
  const map = new Map<string, SessionInfo[]>();
  for (const s of sessions) {
    const list = map.get(s.agent_name) ?? [];
    list.push(s);
    map.set(s.agent_name, list);
  }
  return [...map.entries()];
}

function basename(path: string) {
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}
