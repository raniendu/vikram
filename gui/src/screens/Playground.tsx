import { useEffect, useRef, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AgentSummary,
  ColumnMetrics,
  ProviderInfo,
  listAgents,
  listProviders,
  startComparison,
} from "../api/client";
import { StreamEvent, streamSession } from "../api/events";

const MAX = 4;

interface ColumnState {
  provider: string;
  model: string;
  text: string;
  thinking: string;
  metrics: ColumnMetrics | null;
}

export function Playground() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [agentId, setAgentId] = useState("");
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [columns, setColumns] = useState<ColumnState[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const handle = useRef<{ close: () => void } | null>(null);

  useEffect(() => {
    listAgents().then((list) => {
      setAgents(list);
      if (list[0]) setAgentId(list[0].id);
    });
    listProviders().then((p) => {
      setProviders(p);
      const first = p.find((x) => x.configured_model)?.id ?? "ollama";
      setColumns([
        { provider: first, model: "", text: "", thinking: "", metrics: null },
        { provider: first, model: "", text: "", thinking: "", metrics: null },
      ]);
    });
    return () => handle.current?.close();
  }, []);

  function patch(index: number, values: Partial<ColumnState>) {
    setColumns((prev) =>
      prev.map((c, i) => (i === index ? { ...c, ...values } : c)),
    );
  }

  async function run() {
    if (!workspace || !prompt.trim()) return;
    setError(null);
    setColumns((prev) =>
      prev.map((c) => ({ ...c, text: "", thinking: "", metrics: null })),
    );
    setRunning(true);
    try {
      const info = await startComparison(
        agentId,
        workspace,
        prompt,
        columns.map((c) => ({ provider: c.provider, model: c.model })),
      );
      handle.current = streamSession(info.session_id, onEvent, (e) => {
        setError(String(e));
        setRunning(false);
      });
    } catch (e) {
      setError(String(e));
      setRunning(false);
    }
  }

  function onEvent(event: StreamEvent) {
    const id = event.column_id;
    if (event.type === "turn.finished") {
      setRunning(false);
      const byId = new Map<string, ColumnMetrics>(
        (event.payload.columns ?? []).map((m: ColumnMetrics) => [
          m.column_id,
          m,
        ]),
      );
      return setColumns((prev) =>
        prev.map((c) => ({
          ...c,
          metrics: byId.get(`${c.provider}/${c.model}`) ?? c.metrics,
        })),
      );
    }
    if (!id) return;
    const index = columns.findIndex((c) => `${c.provider}/${c.model}` === id);
    if (index < 0) return;

    if (event.type === "text.delta")
      return setColumns((prev) =>
        prev.map((c, i) =>
          i === index ? { ...c, text: c.text + event.payload.text } : c,
        ),
      );
    if (event.type === "thinking.delta")
      return setColumns((prev) =>
        prev.map((c, i) =>
          i === index ? { ...c, thinking: c.thinking + event.payload.text } : c,
        ),
      );
  }

  const ready =
    workspace && prompt.trim() && columns.every((c) => c.model.trim());

  return (
    <div className="stack">
      <header className="screen-head">
        <h1>Playground</h1>
        <p className="muted">
          One agent, one prompt, {columns.length} models side by side.
          Approval-gated tools are disabled here so the columns cannot fight
          over the workspace.
        </p>
      </header>

      <div className="row wrap">
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          {agents
            .filter((a) => !a.error)
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
        </select>
        <button
          className="secondary"
          onClick={async () => {
            const chosen = await open({ directory: true, multiple: false });
            if (typeof chosen === "string") setWorkspace(chosen);
          }}
        >
          {workspace ? "Change folder" : "Choose folder…"}
        </button>
        <code className="path">{workspace ?? "No folder chosen"}</code>
      </div>

      <div className="row wrap">
        {columns.map((column, i) => (
          <div key={i} className="row">
            <select
              value={column.provider}
              onChange={(e) => patch(i, { provider: e.target.value })}
            >
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.id}
                </option>
              ))}
            </select>
            <input
              value={column.model}
              placeholder="model name"
              onChange={(e) => patch(i, { model: e.target.value })}
            />
            {columns.length > 2 && (
              <button
                className="link"
                onClick={() => setColumns(columns.filter((_, j) => j !== i))}
              >
                ✕
              </button>
            )}
          </div>
        ))}
        {columns.length < MAX && (
          <button
            className="secondary"
            onClick={() =>
              setColumns([
                ...columns,
                {
                  provider: columns[0]?.provider ?? "ollama",
                  model: "",
                  text: "",
                  thinking: "",
                  metrics: null,
                },
              ])
            }
          >
            Add model
          </button>
        )}
      </div>

      <textarea
        rows={3}
        value={prompt}
        placeholder="One prompt, sent to every model…"
        onChange={(e) => setPrompt(e.target.value)}
      />
      <div>
        <button disabled={!ready || running} onClick={run}>
          {running ? "Running…" : "Compare"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {(running || columns.some((c) => c.text || c.metrics)) && (
        <div className="columns">
          {columns.map((column, i) => (
            <article key={i} className="column">
              <header>
                <code>{column.model || "—"}</code>
                {column.metrics && (
                  <div className="metrics">
                    <span title="time to first token">
                      ttft {fmt(column.metrics.ttft_ms)}
                    </span>
                    <span title="total">{fmt(column.metrics.total_ms)}</span>
                    <span title="tokens">
                      {column.metrics.total_tokens ?? "?"} tok
                    </span>
                  </div>
                )}
              </header>
              {column.metrics?.error && (
                <p className="error small">{column.metrics.error}</p>
              )}
              {column.thinking && (
                <details>
                  <summary className="muted small">thinking</summary>
                  <pre className="muted small">{column.thinking}</pre>
                </details>
              )}
              <div className="column-body">{column.text}</div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function fmt(ms: number | null | undefined) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}
