import { useEffect, useState } from "react";
import { AgentSummary, listAgents } from "../api/client";

interface Props {
  onChat: (agent: AgentSummary) => void;
}

export function AgentList({ onChat }: Props) {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAgents().then(setAgents).catch((e) => setError(String(e)));
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!agents) return <p className="muted">Loading agents…</p>;

  return (
    <div className="stack">
      <header className="screen-head">
        <h1>Agents</h1>
        <p className="muted">
          Built-in agents ship with Vikram and are read-only. Editing one keeps
          your copy separate.
        </p>
      </header>

      <div className="cards">
        {agents.map((agent) => (
          <article key={agent.id} className="card">
            <div className="card-head">
              <h2>{agent.name}</h2>
              <span className={`badge badge-${agent.root}`}>{agent.root}</span>
              {agent.cli_only && <span className="badge">local only</span>}
            </div>
            <p className="muted">{agent.description || <em>No description</em>}</p>

            {agent.error ? (
              <p className="error">Spec will not load: {agent.error}</p>
            ) : (
              <dl className="facts">
                <div>
                  <dt>Model</dt>
                  <dd>{agent.resolved_model ?? "—"}</dd>
                </div>
                <div>
                  <dt>Tools</dt>
                  <dd>{agent.tools.length}</dd>
                </div>
                <div>
                  <dt>MCP</dt>
                  <dd>{agent.mcp_server_count}</dd>
                </div>
              </dl>
            )}

            <div className="card-actions">
              <button disabled={!!agent.error} onClick={() => onChat(agent)}>
                Chat
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
