import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { AgentSummary, listAgents, openSession } from "../api/client";

interface Props {
  /** Preselected when arriving from an agent row; null from "+ New session". */
  agent: AgentSummary | null;
  onCancel: () => void;
  onStarted: (sessionId: string) => void;
}

export function NewSession({ agent, onCancel, onStarted }: Props) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentId, setAgentId] = useState(agent?.id ?? "");
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAgents()
      .then((list) => {
        setAgents(list);
        if (!agentId && list[0]) setAgentId(list[0].id);
      })
      .catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function start() {
    if (!agentId || !workspace) return;
    setStarting(true);
    setError(null);
    try {
      const info = await openSession(agentId, workspace);
      onStarted(info.session_id);
    } catch (e) {
      setError(String(e));
      setStarting(false);
    }
  }

  return (
    <div className="screen">
      <h1 className="page">New session</h1>
      <p className="lede">
        One agent in one folder, in its own process. Its file and shell tools are
        scoped to that folder.
      </p>

      <div className="rule" />

      <div className="fields" style={{ marginTop: 16 }}>
        <label className="field">
          <span className="eyebrow">Agent</span>
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            {agents
              .filter((a) => !a.error)
              .map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} — {a.resolved_model ?? "no model"}
                </option>
              ))}
          </select>
        </label>

        <label className="field">
          <span className="eyebrow">Folder</span>
          <div className="row">
            <button
              className="btn secondary"
              onClick={async () => {
                const chosen = await open({ directory: true, multiple: false });
                if (typeof chosen === "string") setWorkspace(chosen);
              }}
            >
              {workspace ? "Change" : "Choose…"}
            </button>
            <span className="path">{workspace ?? "None chosen"}</span>
          </div>
        </label>
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <button className="btn" disabled={!workspace || !agentId || starting} onClick={start}>
          {starting ? "Starting…" : "Start session"}
        </button>
        <button className="btn quiet" onClick={onCancel}>
          Cancel
        </button>
      </div>

      {error && <p className="error" style={{ marginTop: 12 }}>{error}</p>}
    </div>
  );
}
