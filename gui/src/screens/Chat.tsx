import { useEffect, useRef, useState } from "react";
import {
  SessionInfo,
  answerApproval,
  cancelTurn,
  closeSession,
  sendPrompt,
} from "../api/client";
import { StreamEvent, streamSession } from "../api/events";
import { Eyebrow } from "../components/primitives";

interface Props {
  sessionId: string;
  /** Summary from the rail's poll; null for a beat after a fresh start. */
  session: SessionInfo | null;
  onEnded: () => void;
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

export function Chat({ sessionId, session, onEnded }: Props) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const stream = useRef<{ close: () => void } | null>(null);
  const answered = useRef<Set<string>>(new Set());

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [entries, approval]);

  useEffect(() => {
    stream.current?.close();
    stream.current = streamSession(sessionId, handleEvent, (e) => setError(String(e)));
    return () => stream.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // An approval raised before this view attached is not in the replay backlog,
  // so the stream alone would leave it invisible and unanswerable. The session
  // summary carries it — but never re-adopt one already answered here, since
  // the summary poll lags the answer and would put the card straight back.
  useEffect(() => {
    if (approval) return;
    const pending = session?.pending_approval;
    if (pending && !answered.current.has(pending.approval_id)) setApproval(pending);
  }, [session, approval]);

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

  async function submit() {
    if (!draft.trim()) return;
    const text = draft;
    setDraft("");
    setEntries((prev) => [...prev, { kind: "you", text }]);
    setBusy(true);
    try {
      await sendPrompt(sessionId, text);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  async function decide(decision: "allow" | "deny") {
    if (!approval) return;
    answered.current.add(approval.approval_id);
    setApproval(null);
    await answerApproval(sessionId, approval.approval_id, decision, approval.tool_name);
  }

  const agentName = session?.agent_name ?? "Agent";

  return (
    <section className="session">
      <div className="session-head">
        <div className="row" style={{ minWidth: 0, gap: 12 }}>
          <span className="mono truncate" style={{ fontSize: 12.5 }}>
            {session?.workspace ?? "…"}
          </span>
          <Eyebrow style={{ fontSize: 9 }}>{agentName}</Eyebrow>
          <Eyebrow style={{ fontSize: 9 }}>{session?.model ?? ""}</Eyebrow>
          {session?.state === "needs" && (
            <Eyebrow className="accent" style={{ fontSize: 9 }}>Needs you</Eyebrow>
          )}
        </div>
        <button
          className="btn quiet"
          onClick={async () => {
            await closeSession(sessionId);
            onEnded();
          }}
        >
          End
        </button>
      </div>

      <div className="transcript" ref={scroller}>
        <div className="turns">
          {entries.length === 0 && !approval && (
            <p className="muted small">
              {session && session.turns > 0
                ? "Rejoined. Earlier turns are not replayed — send a prompt to carry on."
                : "Send a prompt to start."}
            </p>
          )}

          {entries.length === 0 && approval && (
            <p className="muted small">Rejoined. It is waiting on the call below.</p>
          )}

          {entries.map((entry, i) => (
            <Bubble key={i} entry={entry} agentName={agentName} />
          ))}

          {approval && (
            <div className="approval">
              <div className="approval-head">
                <Eyebrow className="accent" style={{ fontSize: 9 }}>
                  Approval required
                </Eyebrow>
                <span className="mono" style={{ fontSize: 11, color: "var(--muted-2)" }}>
                  {approval.tool_name}
                </span>
              </div>
              <div className="approval-body">
                <div className="approval-grid">
                  {Object.entries(approval.input).map(([key, value]) => (
                    <ArgRow key={key} label={key} value={value} />
                  ))}
                </div>
                <div className="row" style={{ marginTop: 12 }}>
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

      {error && <p className="error small" style={{ padding: "0 var(--gutter)" }}>{error}</p>}

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
            <button type="button" className="btn secondary" onClick={() => cancelTurn(sessionId)}>
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
  );
}

function ArgRow({ label, value }: { label: string; value: unknown }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <>
      <Eyebrow style={{ fontSize: 9 }}>{label}</Eyebrow>
      <pre>{text}</pre>
    </>
  );
}

function Bubble({ entry, agentName }: { entry: Entry; agentName: string }) {
  if (entry.kind === "you")
    return (
      <div className="turn-you">
        <Eyebrow style={{ fontSize: 9, marginBottom: 3 }}>You</Eyebrow>
        <p>{entry.text}</p>
      </div>
    );
  if (entry.kind === "think")
    return (
      <div className="turn-think">
        <Eyebrow style={{ fontSize: 9, marginBottom: 3 }}>Thinking</Eyebrow>
        <p>{entry.text}</p>
      </div>
    );
  if (entry.kind === "note")
    return <Eyebrow style={{ fontSize: 9 }}>{entry.text}</Eyebrow>;
  if (entry.kind === "tool")
    return (
      <div>
        <div className="tool-head">
          <span
            className="mono"
            style={{
              fontSize: 12,
              color: entry.status === "error" ? "var(--muted-2)" : "var(--accent)",
            }}
          >
            {entry.status === "error" ? "✗" : entry.status ? "✓" : "→"}
          </span>
          <span className="mono" style={{ fontSize: 11.5 }}>{entry.name}</span>
        </div>
        {entry.detail && <pre className="tool-out">{entry.detail.slice(0, 1200)}</pre>}
      </div>
    );
  return (
    <div className="turn-say">
      <Eyebrow style={{ fontSize: 9, marginBottom: 3 }}>{agentName}</Eyebrow>
      <p>{entry.text}</p>
    </div>
  );
}
