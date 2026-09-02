import { useEffect, useState } from "react";
import {
  SessionInfo,
  SessionState,
  answerApproval,
  closeSession,
  listSessions,
} from "../api/client";
import { Eyebrow, Rule } from "../components/primitives";

interface Props {
  onOpen: (sessionId: string) => void;
}

type Filter = "all" | SessionState;

const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["needs", "Needs you"],
  ["running", "Running"],
  ["idle", "Idle"],
];

const STATE_LABEL: Record<SessionState, string> = {
  needs: "Needs you",
  running: "Running",
  idle: "Idle",
};

export function Sessions({ onOpen }: Props) {
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [waiting, setWaiting] = useState(0);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const payload = await listSessions();
      setSessions(payload.sessions);
      setWaiting(payload.waiting);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refresh();
    // The list is a summary of live processes, so it has to keep up on its
    // own — nothing pushes to this screen.
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!sessions) return <p className="muted">Loading sessions…</p>;

  const shown = filter === "all" ? sessions : sessions.filter((s) => s.state === filter);
  const live = sessions.filter((s) => s.state !== "idle").length;

  return (
    <div className="screen">
      <div className="head-row">
        <div>
          <h1 className="page">Sessions</h1>
          <p className="lede">
            Every session is one agent in one folder, in its own process. The same
            agent can run in several folders at once — the folder is what tells
            them apart.
          </p>
        </div>
        {sessions.length > 0 && (
          <div className="stat">
            <div className="n">{live}</div>
            <Eyebrow style={{ marginTop: 3 }}>
              {waiting ? `live · ${waiting} waiting on you` : "live"}
            </Eyebrow>
          </div>
        )}
      </div>

      <div className="rule" />

      <div className="filters">
        {FILTERS.map(([key, label]) => (
          <button
            key={key}
            className={`eyebrow${filter === key ? " active" : ""}`}
            onClick={() => setFilter(key)}
          >
            {label}
          </button>
        ))}
        <div className="grow" />
        <Eyebrow>Sorted by last activity</Eyebrow>
      </div>

      <Rule />

      {shown.length === 0 && (
        <p className="muted" style={{ paddingTop: 26 }}>
          {sessions.length === 0
            ? "No sessions running. Open one from an agent."
            : "Nothing in this state."}
        </p>
      )}

      {shown.map((session) => (
        <div key={session.session_id} className="list-row sessions">
          <div>
            <Eyebrow className={session.state === "needs" ? "accent" : undefined}>
              {STATE_LABEL[session.state]}
            </Eyebrow>
            <div className="row-title" style={{ fontSize: 19, marginTop: 8 }}>
              {session.agent_name}
            </div>
            <div className="mono" style={{ fontSize: 12, color: "var(--muted-2)", marginTop: 3 }}>
              {session.model ?? "—"}
            </div>
          </div>

          {/* The folder is what makes this session distinct, so it takes the
              title slot and the agent drops to the rail. */}
          <div>
            <button className="row-path" onClick={() => onOpen(session.session_id)}>
              {session.workspace}
            </button>
            <p
              className="row-body"
              style={session.state === "needs" ? { color: "var(--fg)" } : undefined}
            >
              {session.activity || "Waiting for a first prompt."}
            </p>

            {session.pending_approval && (
              <div className="row" style={{ marginTop: 13 }}>
                <button
                  className="btn"
                  style={{ padding: "7px 16px", fontSize: 13.5 }}
                  onClick={async () => {
                    await answerApproval(
                      session.session_id,
                      session.pending_approval!.approval_id,
                      "allow",
                      session.pending_approval!.tool_name,
                    );
                    refresh();
                  }}
                >
                  Allow
                </button>
                <button
                  className="btn secondary"
                  style={{ padding: "7px 15px", fontSize: 13.5 }}
                  onClick={async () => {
                    await answerApproval(
                      session.session_id,
                      session.pending_approval!.approval_id,
                      "deny",
                      session.pending_approval!.tool_name,
                    );
                    refresh();
                  }}
                >
                  Deny
                </button>
                <span className="mono small" style={{ color: "var(--muted-2)" }}>
                  {session.pending_approval.tool_name}
                </span>
              </div>
            )}
          </div>

          <div className="row-rail">
            <Eyebrow>{formatAgo(session.elapsed_ms)}</Eyebrow>
            <Eyebrow>{session.turns === 1 ? "1 turn" : `${session.turns} turns`}</Eyebrow>
            <button
              className="eyebrow"
              onClick={async () => {
                await closeSession(session.session_id);
                refresh();
              }}
            >
              End
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

export function formatAgo(ms: number) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s ago`;
  return `${Math.floor(m / 60)}h ${m % 60}m ago`;
}
