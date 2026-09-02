import { useEffect, useRef, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AgentSummary,
  SessionInfo,
  answerApproval,
  cancelTurn,
  closeSession,
  openSession,
  sendPrompt,
} from "../api/client";
import { StreamEvent, streamSession } from "../api/events";

interface Props {
  agent: AgentSummary;
  onBack: () => void;
}

interface Approval {
  approval_id: string;
  tool_name: string;
  input: Record<string, unknown>;
}

type Entry =
  | { kind: "user"; text: string }
  | { kind: "text"; text: string }
  | { kind: "thinking"; text: string }
  | { kind: "tool"; name: string; status?: string; detail?: string }
  | { kind: "note"; text: string };

export function Chat({ agent, onBack }: Props) {
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [entries, approval]);

  async function pickWorkspace() {
    const chosen = await open({ directory: true, multiple: false });
    if (typeof chosen === "string") setWorkspace(chosen);
  }

  async function start() {
    if (!workspace) return;
    setError(null);
    try {
      const info = await openSession(agent.id, workspace);
      setSession(info);
      streamSession(info.session_id, handleEvent, (e) => setError(String(e)));
    } catch (e) {
      setError(String(e));
    }
  }

  function handleEvent(event: StreamEvent) {
    const p = event.payload;
    switch (event.type) {
      case "text.delta":
        return appendStreaming("text", p.text);
      case "thinking.delta":
        return appendStreaming("thinking", p.text);
      case "tool.call":
        return setEntries((prev) => [
          ...prev,
          { kind: "tool", name: p.name },
        ]);
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
        // `auto` approvals are already granted; they are shown, not asked.
        if (!p.auto) setApproval(p as Approval);
        return;
      case "approval.resolved":
        setApproval(null);
        return setEntries((prev) => [
          ...prev,
          { kind: "note", text: `Approval ${p.decision}` },
        ]);
      case "turn.finished":
        setBusy(false);
        return setEntries((prev) => [
          ...prev,
          {
            kind: "note",
            text: `Finished in ${Math.round(p.duration_ms)}ms · ${
              p.usage?.total_tokens ?? "?"
            } tokens`,
          },
        ]);
      case "turn.failed":
        setBusy(false);
        return setEntries((prev) => [
          ...prev,
          { kind: "note", text: `Failed: ${p.error}` },
        ]);
      case "turn.cancelled":
        setBusy(false);
        return setEntries((prev) => [
          ...prev,
          { kind: "note", text: "Cancelled" },
        ]);
    }
  }

  function appendStreaming(kind: "text" | "thinking", text: string) {
    setEntries((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.kind === kind) {
        return [...prev.slice(0, -1), { kind, text: last.text + text }];
      }
      return [...prev, { kind, text }];
    });
  }

  async function submit() {
    if (!session || !draft.trim()) return;
    const text = draft;
    setDraft("");
    setEntries((prev) => [...prev, { kind: "user", text }]);
    setBusy(true);
    try {
      await sendPrompt(session.session_id, text);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  async function decide(decision: "allow" | "deny") {
    if (!session || !approval) return;
    await answerApproval(
      session.session_id,
      approval.approval_id,
      decision,
      approval.tool_name,
    );
    setApproval(null);
  }

  if (!session) {
    return (
      <div className="stack">
        <button className="link" onClick={onBack}>
          ← Agents
        </button>
        <h1>{agent.name}</h1>
        <p className="muted">
          Choose the folder this agent should work in. Its file and shell tools
          are scoped to that folder.
        </p>
        <div className="row">
          <button onClick={pickWorkspace}>Choose folder…</button>
          <code className="path">{workspace ?? "No folder chosen"}</code>
        </div>
        <div>
          <button disabled={!workspace} onClick={start}>
            Start session
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </div>
    );
  }

  return (
    <div className="chat">
      <header className="chat-head">
        <button className="link" onClick={onBack}>
          ← Agents
        </button>
        <div>
          <strong>{agent.name}</strong>{" "}
          <span className="muted">
            {session.model_config?.model} · {session.workspace}
          </span>
        </div>
        <button
          className="link"
          onClick={() => closeSession(session.session_id).then(onBack)}
        >
          End session
        </button>
      </header>

      <div className="transcript" ref={scroller}>
        {entries.map((entry, i) => (
          <Bubble key={i} entry={entry} />
        ))}

        {approval && (
          <div className="approval">
            <h3>Approve {approval.tool_name}?</h3>
            <pre>{JSON.stringify(approval.input, null, 2)}</pre>
            <div className="row">
              <button onClick={() => decide("allow")}>Allow</button>
              <button className="secondary" onClick={() => decide("deny")}>
                Deny
              </button>
            </div>
          </div>
        )}
      </div>

      {error && <p className="error">{error}</p>}

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
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
            className="secondary"
            onClick={() => cancelTurn(session.session_id)}
          >
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!draft.trim()}>
            Send
          </button>
        )}
      </form>
    </div>
  );
}

function Bubble({ entry }: { entry: Entry }) {
  if (entry.kind === "user")
    return <div className="bubble user">{entry.text}</div>;
  if (entry.kind === "thinking")
    return <div className="bubble thinking">{entry.text}</div>;
  if (entry.kind === "note")
    return <div className="bubble note">{entry.text}</div>;
  if (entry.kind === "tool")
    return (
      <div className="bubble tool">
        <code>
          {entry.status === "error" ? "✗" : entry.status ? "✓" : "→"}{" "}
          {entry.name}
        </code>
        {entry.detail && <pre>{entry.detail.slice(0, 600)}</pre>}
      </div>
    );
  return <div className="bubble assistant">{entry.text}</div>;
}
