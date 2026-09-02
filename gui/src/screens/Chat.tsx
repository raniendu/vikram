import { useEffect, useRef, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AgentSummary,
  SessionInfo,
  answerApproval,
  cancelTurn,
  closeSession,
  listSessions,
  openSession,
  sendPrompt,
} from "../api/client";
import { StreamEvent, streamSession } from "../api/events";
import { Dot, Eyebrow } from "../components/primitives";

interface Props {
  /** Set when starting a fresh session from the agent list; null on the
      Sessions tab, where the rail is the whole navigation. */
  agent: AgentSummary | null;
  onBack: () => void;
}

interface Approval {
  approval_id: string;
  tool_name: string;
  input: Record<string, unknown>;
}

type Entry =
  | { kind: "you"; text: string }
  | { kind: "say"; text: string }
  | { kind: "think"; text: string }
  | { kind: "tool"; name: string; status?: string; detail?: string }
  | { kind: "note"; text: string };

export function Chat({ agent, onBack }: Props) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const stream = useRef<{ close: () => void } | null>(null);
  const answered = useRef<Set<string>>(new Set());

  async function refreshSessions() {
    try {
      const live = (await listSessions()).sessions;
      setSessions(live);
      return live;
    } catch {
      /* the rail simply stays as it was */
      return null;
    }
  }

  useEffect(() => {
    // Arriving on the Sessions tab with no session chosen: open whichever one
    // is asking for something, else the most recently active.
    refreshSessions().then((live) => {
      if (agent || current || !live) return;
      const pick = live.find((s) => s.state === "needs") ?? live[0];
      if (pick) setCurrent(pick.session_id);
    });
    const id = setInterval(refreshSessions, 3000);
    return () => {
      clearInterval(id);
      stream.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [entries, approval]);

  // Attaching to a session replays session.ready from the backlog, so a
  // resumed session knows what it is even though its transcript is gone.
  useEffect(() => {
    if (!current) return;
    setEntries([]);
    setApproval(null);
    stream.current?.close();
    stream.current = streamSession(current, handleEvent, (e) => setError(String(e)));
    return () => stream.current?.close();
  }, [current]);

  // An approval raised before this view attached is not in the replay backlog,
  // so the stream alone would leave it invisible and unanswerable. The session
  // summary carries it, so adopt it on arrival — but never re-adopt one this
  // view has already answered, since the summary poll lags the answer by up to
  // its interval and would put the card straight back.
  useEffect(() => {
    if (!current || approval) return;
    const pending = sessions.find((s) => s.session_id === current)?.pending_approval;
    if (pending && !answered.current.has(pending.approval_id)) setApproval(pending);
  }, [current, sessions, approval]);

  function handleEvent(event: StreamEvent) {
    const p = event.payload;
    switch (event.type) {
      case "session.ready":
        return;
      case "text.delta":
        return appendStreaming("say", p.text);
      case "thinking.delta":
        return appendStreaming("think", p.text);
      case "tool.call":
        setBusy(true);
        return setEntries((prev) => [...prev, { kind: "tool", name: p.name }]);
      case "tool.result":
        return setEntries((prev) => {
          const next = [...prev];
          for (let i = next.length - 1; i >= 0; i--) {
            const entry = next[i];
            if (entry && entry.kind === "tool" && !entry.status) {
              next[i] = { ...entry, status: p.status, detail: p.text };
              break;
            }
          }
          return next;
        });
      case "approval.requested":
        if (!p.auto) setApproval(p as Approval);
        return;
      case "approval.resolved":
        if (p.approval_id) answered.current.add(p.approval_id);
        setApproval(null);
        return setEntries((prev) => [
          ...prev,
          { kind: "note", text: `Approval ${p.decision}` },
        ]);
      case "turn.started":
        return setBusy(true);
      case "turn.finished": {
        setBusy(false);
        refreshSessions();
        const tokens = p.usage?.total_tokens;
        return setEntries((prev) => [
          ...prev,
          {
            kind: "note",
            text: `${Math.round(p.duration_ms) / 1000}s${tokens ? ` · ${tokens} tokens` : ""}`,
          },
        ]);
      }
      case "turn.failed":
        setBusy(false);
        return setEntries((prev) => [...prev, { kind: "note", text: `Failed: ${p.error}` }]);
      case "turn.cancelled":
        setBusy(false);
        return setEntries((prev) => [...prev, { kind: "note", text: "Cancelled" }]);
    }
  }

  function appendStreaming(kind: "say" | "think", text: string) {
    setEntries((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.kind === kind) {
        return [...prev.slice(0, -1), { kind, text: last.text + text }];
      }
      return [...prev, { kind, text }];
    });
  }

  async function start() {
    if (!agent || !workspace) return;
    setError(null);
    try {
      const info = await openSession(agent.id, workspace);
      await refreshSessions();
      setCurrent(info.session_id);
    } catch (e) {
      setError(String(e));
    }
  }

  async function submit() {
    if (!current || !draft.trim()) return;
    const text = draft;
    setDraft("");
    setEntries((prev) => [...prev, { kind: "you", text }]);
    setBusy(true);
    try {
      await sendPrompt(current, text);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  async function decide(decision: "allow" | "deny") {
    if (!current || !approval) return;
    answered.current.add(approval.approval_id);
    setApproval(null);
    await answerApproval(current, approval.approval_id, decision, approval.tool_name);
    refreshSessions();
  }

  if (!current && !agent) {
    return (
      <div className="screen">
        <h1 className="page">Sessions</h1>
        <p className="lede">
          Every session is one agent in one folder, in its own process. The same
          agent can run in several folders at once — the folder is what tells them
          apart.
        </p>
        <div className="rule" />
        <p className="muted" style={{ paddingTop: 26 }}>
          Nothing running. Pick an agent and choose a folder to start one.
        </p>
        <div style={{ marginTop: 18 }}>
          <button className="btn" onClick={onBack}>
            Go to agents
          </button>
        </div>
      </div>
    );
  }

  // Choosing a folder, before this session exists.
  if (!current) {
    return (
      <div className="screen">
        <button className="btn quiet" onClick={onBack}>
          ← Agents
        </button>
        <h1 className="page" style={{ marginTop: 8 }}>{agent?.name ?? "New session"}</h1>
        <p className="lede">
          Choose the folder this agent works in. Its file and shell tools are scoped
          to that folder, and the session gets its own process.
        </p>
        <div className="rule" />
        <div className="row" style={{ marginTop: 24 }}>
          <button
            className="btn secondary"
            onClick={async () => {
              const chosen = await open({ directory: true, multiple: false });
              if (typeof chosen === "string") setWorkspace(chosen);
            }}
          >
            {workspace ? "Change folder" : "Choose folder…"}
          </button>
          <span className="path">{workspace ?? "No folder chosen"}</span>
        </div>
        <div style={{ marginTop: 20 }}>
          <button className="btn" disabled={!workspace} onClick={start}>
            Start session
          </button>
        </div>
        {error && <p className="error" style={{ marginTop: 16 }}>{error}</p>}
      </div>
    );
  }

  const active = sessions.find((s) => s.session_id === current);
  const groups = groupByAgent(sessions);
  const waiting = sessions.filter((s) => s.state === "needs").length;

  return (
    <div className="with-rail">
      {/* 18rem — the site's own --rail-width. Grouped by agent so a dozen
          sessions of one agent stay a single scannable block. */}
      <aside className="rail">
        <div className="rail-head">
          <Eyebrow>Sessions</Eyebrow>
          <Eyebrow className={waiting ? "accent" : undefined}>
            {waiting ? `${sessions.length} · ${waiting} waiting` : String(sessions.length)}
          </Eyebrow>
        </div>

        <div className="rail-list">
          {groups.map(([agentName, items]) => (
            <div key={agentName}>
              <div className="rail-group">
                <Eyebrow style={{ color: "var(--fg)" }}>{agentName}</Eyebrow>
                <Eyebrow>{items.length}</Eyebrow>
              </div>
              {items.map((s) => (
                <button
                  key={s.session_id}
                  className={`rail-item${s.session_id === current ? " active" : ""}`}
                  onClick={() => setCurrent(s.session_id)}
                  title={s.workspace}
                >
                  <Dot live={s.state !== "idle"} />
                  <span className="rail-item-text">
                    <span className="name">{basename(s.workspace)}</span>
                    <span className="rail-item-sub">
                      {s.state === "needs"
                        ? "Waiting on you"
                        : s.activity || formatAgo(s.elapsed_ms)}
                    </span>
                  </span>
                  {s.state === "needs" && (
                    <span className="eyebrow accent" style={{ fontSize: 11 }}>!</span>
                  )}
                </button>
              ))}
            </div>
          ))}
          {sessions.length === 0 && (
            <p className="muted small rail-empty">No sessions running.</p>
          )}
        </div>

        <div className="rail-foot">
          <button className="btn quiet" onClick={onBack}>
            + New session
          </button>
        </div>
      </aside>

      <section className="session">
        <div className="session-head">
          <div style={{ minWidth: 0 }}>
            <div className="row-path" style={{ fontSize: 16 }}>
              {active?.workspace ?? "…"}
            </div>
            <div className="row" style={{ gap: 15, marginTop: 4 }}>
              <Eyebrow>{active?.agent_name ?? agent?.name ?? ""}</Eyebrow>
              <Eyebrow>{active?.model ?? ""}</Eyebrow>
            </div>
          </div>
          <button
            className="btn quiet"
            onClick={async () => {
              await closeSession(current);
              await refreshSessions();
              onBack();
            }}
          >
            End session
          </button>
        </div>

        <div className="transcript" ref={scroller}>
          <div className="turns">
            {entries.length === 0 && !approval && (
              <p className="muted">
                {active && active.turns > 0
                  ? "Rejoined this session. Earlier turns are not replayed — send a prompt to carry on."
                  : "Send a prompt to start."}
              </p>
            )}

            {entries.length === 0 && approval && (
              <p className="muted">
                Rejoined this session. It is waiting on the call below.
              </p>
            )}

            {entries.map((entry, i) => (
              <Bubble key={i} entry={entry} agentName={active?.agent_name ?? "Agent"} />
            ))}

            {approval && (
              <div className="approval">
                <div className="approval-head">
                  <Eyebrow className="accent">Approval required</Eyebrow>
                  <span className="mono small" style={{ color: "var(--muted-2)" }}>
                    {approval.tool_name}
                  </span>
                </div>
                <div className="approval-body">
                  <div className="approval-grid">
                    {Object.entries(approval.input).map(([key, value]) => (
                      <FragmentRow key={key} label={key} value={value} />
                    ))}
                  </div>
                  <div className="row" style={{ marginTop: 18 }}>
                    <button className="btn" onClick={() => decide("allow")}>
                      Allow
                    </button>
                    <button className="btn secondary" onClick={() => decide("deny")}>
                      Deny
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {error && (
          <p className="error" style={{ padding: "0 var(--gutter)" }}>{error}</p>
        )}

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="composer-inner">
            <textarea
              value={draft}
              placeholder="Ask the agent to do something…"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
              }}
            />
            {busy ? (
              <button
                type="button"
                className="btn secondary"
                onClick={() => cancelTurn(current)}
              >
                Stop
              </button>
            ) : (
              <button type="submit" className="btn" disabled={!draft.trim()}>
                Send
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}

function FragmentRow({ label, value }: { label: string; value: unknown }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <>
      <Eyebrow>{label}</Eyebrow>
      <pre>{text}</pre>
    </>
  );
}

function Bubble({ entry, agentName }: { entry: Entry; agentName: string }) {
  if (entry.kind === "you")
    return (
      <div className="turn-you">
        <Eyebrow style={{ marginBottom: 7 }}>You</Eyebrow>
        <p>{entry.text}</p>
      </div>
    );
  if (entry.kind === "think")
    return (
      <div className="turn-think">
        <Eyebrow style={{ marginBottom: 7 }}>Thinking</Eyebrow>
        <p>{entry.text}</p>
      </div>
    );
  if (entry.kind === "note") return <Eyebrow>{entry.text}</Eyebrow>;
  if (entry.kind === "tool")
    return (
      <div>
        <div className="tool-head">
          <span
            className="mono"
            style={{ color: entry.status === "error" ? "var(--muted-2)" : "var(--accent)" }}
          >
            {entry.status === "error" ? "✗" : entry.status ? "✓" : "→"}
          </span>
          <span className="mono small">{entry.name}</span>
        </div>
        {entry.detail && <pre className="tool-out">{entry.detail.slice(0, 1200)}</pre>}
      </div>
    );
  return (
    <div className="turn-say">
      <Eyebrow style={{ marginBottom: 7 }}>{agentName}</Eyebrow>
      <p>{entry.text}</p>
    </div>
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

export function formatAgo(ms: number) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ${m % 60}m ago`;
}

function basename(path: string) {
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}
