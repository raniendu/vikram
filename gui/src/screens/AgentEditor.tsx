import { useEffect, useState } from "react";
import {
  AgentDetail,
  ToolInfo,
  ValidationReport,
  getAgent,
  listTools,
  saveAgent,
  testMcp,
  validateDraft,
} from "../api/client";

interface Props {
  agentId: string;
  onBack: () => void;
}

type Section = "identity" | "prompt" | "tools" | "model" | "mcp" | "raw";

const SECTIONS: [Section, string][] = [
  ["identity", "Identity"],
  ["prompt", "System prompt"],
  ["tools", "Tools"],
  ["model", "Model"],
  ["mcp", "MCP servers"],
  ["raw", "Raw TOML"],
];

export function AgentEditor({ agentId, onBack }: Props) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [draft, setDraft] = useState<Record<string, any> | null>(null);
  const [prompt, setPrompt] = useState("");
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [section, setSection] = useState<Section>("identity");
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getAgent(agentId), listTools()])
      .then(([d, t]) => {
        setDetail(d);
        setDraft(d.draft);
        setPrompt(d.system_prompt);
        setTools(t);
      })
      .catch((e) => setError(String(e)));
  }, [agentId]);

  if (error) return <p className="error">{error}</p>;
  if (!detail || !draft) return <p className="muted">Loading…</p>;

  const update = (patch: Record<string, any>) =>
    setDraft({ ...draft, ...patch });

  async function validate() {
    setStatus("Validating…");
    try {
      const result = await validateDraft(draft!, agentId);
      setReport(result);
      setStatus(result.ok ? "Valid" : null);
    } catch (e) {
      setError(String(e));
      setStatus(null);
    }
  }

  async function save() {
    setStatus("Saving…");
    try {
      const saved = await saveAgent(agentId, draft!, prompt);
      setDetail(saved);
      setStatus(
        saved.summary.root === "user" && detail?.summary.root === "builtin"
          ? "Saved as your own copy"
          : "Saved",
      );
    } catch (e) {
      setError(String(e));
      setStatus(null);
    }
  }

  return (
    <div className="editor">
      <header className="chat-head">
        <button className="link" onClick={onBack}>
          ← Agents
        </button>
        <div>
          <strong>{detail.summary.name}</strong>{" "}
          <span className="muted">{agentId}</span>
          {detail.summary.root === "builtin" && (
            <span className="badge" style={{ marginLeft: 8 }}>
              editing creates your own copy
            </span>
          )}
        </div>
        <div className="row">
          {status && <span className="muted small">{status}</span>}
          <button className="secondary" onClick={validate}>
            Validate
          </button>
          <button onClick={save}>Save</button>
        </div>
      </header>

      <div className="editor-body">
        <nav className="side">
          {SECTIONS.map(([key, label]) => (
            <button
              key={key}
              className={section === key ? "active" : ""}
              onClick={() => setSection(key)}
            >
              {label}
            </button>
          ))}
        </nav>

        <div className="pane">
          {section === "identity" && (
            <Fields>
              <Field label="Name">
                <input
                  value={draft.name ?? ""}
                  onChange={(e) => update({ name: e.target.value })}
                />
              </Field>
              <Field
                label="Description"
                hint="Shown to orchestrators deciding whether to delegate here."
              >
                <input
                  value={draft.description ?? ""}
                  onChange={(e) => update({ description: e.target.value })}
                />
              </Field>
              <Field
                label="Local only"
                hint="Keeps this agent off the HTTP, threaded and Telegram surfaces."
              >
                <input
                  type="checkbox"
                  checked={!!draft.cli_only}
                  onChange={(e) => update({ cli_only: e.target.checked })}
                />
              </Field>
            </Fields>
          )}

          {section === "prompt" && (
            <Fields>
              <Field label="System prompt" hint={`Saved to ${draft.system_prompt}`}>
                <textarea
                  className="code"
                  rows={22}
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </Field>
              {report?.system_prompt && (
                <details>
                  <summary className="muted">
                    Assembled prompt ({report.system_prompt.length} chars) — what
                    the model actually sees
                  </summary>
                  <pre className="assembled">{report.system_prompt}</pre>
                </details>
              )}
            </Fields>
          )}

          {section === "tools" && (
            <div className="tool-grid">
              {tools.map((tool) => {
                const on = (draft.tools ?? []).includes(tool.name);
                return (
                  <label key={tool.name} className="tool">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={(e) =>
                        update({
                          tools: e.target.checked
                            ? [...(draft.tools ?? []), tool.name]
                            : (draft.tools ?? []).filter(
                                (t: string) => t !== tool.name,
                              ),
                        })
                      }
                    />
                    <div>
                      <div className="row">
                        <code>{tool.name}</code>
                        {tool.requires_approval && (
                          <span className="badge">needs approval</span>
                        )}
                      </div>
                      <p className="muted small">{tool.description}</p>
                    </div>
                  </label>
                );
              })}
            </div>
          )}

          {section === "model" && (
            <Fields>
              <Field label="Provider" hint="Leave blank to follow your default.">
                <input
                  value={draft.model_provider ?? ""}
                  placeholder="ollama"
                  onChange={(e) =>
                    update({ model_provider: e.target.value || null })
                  }
                />
              </Field>
              <Field label="Model">
                <input
                  value={draft.model ?? ""}
                  placeholder="qwen3.6:35b-mlx"
                  onChange={(e) => update({ model: e.target.value || null })}
                />
              </Field>
              <Field
                label="Model settings"
                hint="JSON: temperature, top_p, max_tokens, thinking…"
              >
                <textarea
                  className="code"
                  rows={7}
                  defaultValue={JSON.stringify(draft.model_settings ?? {}, null, 2)}
                  onBlur={(e) => {
                    try {
                      update({ model_settings: JSON.parse(e.target.value || "{}") });
                      setError(null);
                    } catch {
                      setError("Model settings must be valid JSON.");
                    }
                  }}
                />
              </Field>
            </Fields>
          )}

          {section === "mcp" && (
            <McpEditor
              servers={draft.mcp_servers ?? []}
              onChange={(mcp_servers) => update({ mcp_servers })}
            />
          )}

          {section === "raw" && (
            <Fields>
              <Field
                label="agent.toml"
                hint="Read-only here. The escape hatch is your editor; comments are preserved on save."
              >
                <pre className="assembled">{detail.source_toml}</pre>
              </Field>
            </Fields>
          )}

          {report && report.issues.length > 0 && (
            <div className="issues">
              {report.issues.map((issue, i) => (
                <p key={i} className={issue.severity === "error" ? "error" : "warn"}>
                  <strong>{issue.field ?? "spec"}</strong>: {issue.message}
                  {issue.fix && <span className="muted"> — {issue.fix}</span>}
                </p>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Fields({ children }: { children: React.ReactNode }) {
  return <div className="fields">{children}</div>;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {hint && <span className="muted small">{hint}</span>}
      {children}
    </label>
  );
}

function McpEditor({
  servers,
  onChange,
}: {
  servers: any[];
  onChange: (next: any[]) => void;
}) {
  const [result, setResult] = useState<Record<number, string>>({});

  function patch(index: number, values: Record<string, any>) {
    onChange(servers.map((s, i) => (i === index ? { ...s, ...values } : s)));
  }

  async function test(index: number) {
    setResult({ ...result, [index]: "Testing…" });
    try {
      const r = await testMcp(servers[index]);
      setResult({
        ...result,
        [index]: r.ok ? `${r.tools.length} tools: ${r.tools.join(", ")}` : r.error!,
      });
    } catch (e) {
      setResult({ ...result, [index]: String(e) });
    }
  }

  return (
    <div className="fields">
      {servers.map((server, i) => (
        <div key={i} className="mcp-card">
          <div className="row">
            <input
              value={server.name ?? ""}
              placeholder="name"
              onChange={(e) => patch(i, { name: e.target.value })}
            />
            <select
              value={server.transport ?? "stdio"}
              onChange={(e) => patch(i, { transport: e.target.value })}
            >
              <option value="stdio">stdio</option>
              <option value="http">http</option>
              <option value="sse">sse</option>
            </select>
            <button className="secondary" onClick={() => test(i)}>
              Test
            </button>
            <button
              className="link"
              onClick={() => onChange(servers.filter((_, j) => j !== i))}
            >
              Remove
            </button>
          </div>
          {(server.transport ?? "stdio") === "stdio" ? (
            <div className="row">
              <input
                value={server.command ?? ""}
                placeholder="command (e.g. uvx)"
                onChange={(e) => patch(i, { command: e.target.value })}
              />
              <input
                value={(server.args ?? []).join(" ")}
                placeholder="args"
                onChange={(e) =>
                  patch(i, { args: e.target.value.split(/\s+/).filter(Boolean) })
                }
              />
            </div>
          ) : (
            <input
              value={server.url ?? ""}
              placeholder="https://example.com/mcp"
              onChange={(e) => patch(i, { url: e.target.value })}
            />
          )}
          {result[i] && <p className="muted small">{result[i]}</p>}
        </div>
      ))}
      <div>
        <button
          className="secondary"
          onClick={() =>
            onChange([...servers, { name: "", transport: "stdio", command: "" }])
          }
        >
          Add server
        </button>
      </div>
      <p className="muted small">
        Reference secrets as <code>${"{ENV_VAR}"}</code>; they are never stored in
        the spec or returned by the API.
      </p>
    </div>
  );
}
