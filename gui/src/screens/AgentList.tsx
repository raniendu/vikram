import { useEffect, useState } from "react";
import { AgentSummary, listAgents } from "../api/client";
import { Eyebrow, Rule } from "../components/primitives";

interface Props {
  onChat: (agent: AgentSummary) => void;
  onEdit: (agentId: string) => void;
}

type Filter = "all" | "builtin" | "user";

const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["builtin", "Built-in"],
  ["user", "Yours"],
];

export function AgentList({ onChat, onEdit }: Props) {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAgents()
      .then(setAgents)
      .catch((e) => setError(String(e)));
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!agents) return <p className="muted">Loading agents…</p>;

  const shown = filter === "all" ? agents : agents.filter((a) => a.root === filter);

  return (
    <div className="screen">
      <div className="head-row">
        <div>
          <h1 className="page">Agents</h1>
          <p className="lede">
            Composed from tools, MCP servers and skills. Built-ins ship with Vikram
            and stay read-only — editing one keeps your copy separate.
          </p>
        </div>
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
      </div>

      <Rule />

      {/* Rows with a metadata rail, following the list anatomy on the site —
          not bordered cards. */}
      {shown.map((agent) => (
        <div key={agent.id} className="list-row agents">
          <div>
            <Eyebrow>{agent.root === "user" ? "Yours" : "Built-in"}</Eyebrow>
            <div className="mono" style={{ fontSize: 12.5, marginTop: 9 }}>
              {agent.resolved_model ?? "—"}
            </div>
          </div>

          <div>
            <h2 className="row-title">{agent.name}</h2>
            {agent.error ? (
              <p className="row-body error">Spec will not load: {agent.error}</p>
            ) : (
              <p className="row-body">{agent.description || "No description."}</p>
            )}
            <div className="row-actions">
              <button className="link" disabled={!!agent.error} onClick={() => onChat(agent)}>
                Open session →
              </button>
              <button className="btn quiet" onClick={() => onEdit(agent.id)}>
                Edit
              </button>
            </div>
          </div>

          <div className="row-rail">
            {agent.cli_only && <Eyebrow>Local only</Eyebrow>}
            <Eyebrow>
              {agent.tools.length === 1 ? "1 tool" : `${agent.tools.length} tools`}
            </Eyebrow>
            {agent.mcp_server_count > 0 && (
              <Eyebrow>
                {agent.mcp_server_count === 1 ? "1 MCP" : `${agent.mcp_server_count} MCP`}
              </Eyebrow>
            )}
            {agent.shadows && <Eyebrow>Shadows built-in</Eyebrow>}
          </div>
        </div>
      ))}
    </div>
  );
}
