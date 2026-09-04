import { useEffect, useState } from "react";
import { AgentSummary, listAgents } from "../api/client";
import { Eyebrow, Rule } from "../components/primitives";

interface Props {
  onChat: (agent: AgentSummary) => void;
  onEdit: (agentId: string) => void;
  onCreate: () => void;
}

type Filter = "all" | "builtin" | "user";

const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["builtin", "Built-in"],
  ["user", "Yours"],
];

export function AgentList({ onChat, onEdit, onCreate }: Props) {
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
            Built-ins ship with Vikram and stay read-only — editing one keeps your
            copy separate.
          </p>
        </div>
        <button className="btn" onClick={onCreate}>
          New agent
        </button>
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

      {/* One line per agent: name, model, what it does, what it can reach. */}
      {shown.map((agent) => (
        <div key={agent.id} className="list-row agents">
          <div className="row" style={{ gap: 7 }}>
            <button className="row-title" onClick={() => onChat(agent)}>
              {agent.name}
            </button>
            <Eyebrow style={{ fontSize: 9 }}>
              {agent.root === "user" ? "Yours" : "Built-in"}
            </Eyebrow>
          </div>

          <span className="mono truncate" style={{ fontSize: 11.5, color: "var(--muted)" }}>
            {agent.resolved_model ?? "—"}
          </span>

          {agent.error ? (
            <p className="row-body error">Spec will not load: {agent.error}</p>
          ) : (
            <p className="row-body">{agent.description || "No description."}</p>
          )}

          <div className="row-rail">
            {agent.cli_only && (
              <Eyebrow className="accent" style={{ fontSize: 9 }}>Local only</Eyebrow>
            )}
            <Eyebrow style={{ fontSize: 9 }}>
              {agent.tools.length === 1 ? "1 tool" : `${agent.tools.length} tools`}
            </Eyebrow>
            <button className="eyebrow" style={{ fontSize: 9 }} onClick={() => onEdit(agent.id)}>
              Edit
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
