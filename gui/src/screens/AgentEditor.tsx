import { useEffect, useState } from "react";
import {
  AgentDetail,
  ProviderInfo,
  ToolInfo,
  ValidationReport,
  getAgent,
  listProviders,
  listTools,
  saveAgent,
  validateDraft,
} from "../api/client";
import { McpEditor } from "../components/McpEditor";
import { ModelPicker } from "../components/ModelPicker";
import { ToolList } from "../components/ToolList";
import { Eyebrow, Field, Fields } from "../components/primitives";

interface Props {
  agentId: string;
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

export function AgentEditor({ agentId }: Props) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [draft, setDraft] = useState<Record<string, any> | null>(null);
  const [prompt, setPrompt] = useState("");
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [section, setSection] = useState<Section>("identity");
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getAgent(agentId), listTools(), listProviders()])
      .then(([d, t, p]) => {
        setDetail(d);
        setDraft(d.draft);
        setPrompt(d.system_prompt);
        setTools(t);
        setProviders(p);
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
      <header className="session-head">
        <div>
          <strong>{detail.summary.name}</strong>{" "}
          <span className="muted">{agentId}</span>
          {detail.summary.root === "builtin" && (
            <span className="eyebrow" style={{ marginLeft: 10 }}>
              editing creates your own copy
            </span>
          )}
        </div>
        <div className="row">
          {status && <Eyebrow>{status}</Eyebrow>}
          <button className="btn secondary" onClick={validate}>
            Validate
          </button>
          <button className="btn" onClick={save}>Save</button>
        </div>
      </header>

      <div className="editor-body">
        <nav className="editor-rail">
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
                  <pre className="source">{report.system_prompt}</pre>
                </details>
              )}
            </Fields>
          )}

          {section === "tools" && (
            <ToolList
              tools={tools}
              selected={draft.tools ?? []}
              onChange={(next) => update({ tools: next })}
            />
          )}

          {section === "model" && (
            <Fields>
              <Field
                label="Provider"
                hint="Leave blank to follow your default. Changing it refetches the model list."
              >
                <select
                  value={draft.model_provider ?? ""}
                  onChange={(e) =>
                    update({ model_provider: e.target.value || null })
                  }
                >
                  <option value="">Follow my default</option>
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.display_name}
                      {p.needs_api_key && !p.has_credential ? " — no key" : ""}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Model"
                hint={
                  draft.model_provider
                    ? "Read from the provider above. A name you type by hand is still accepted."
                    : "Pick a provider to list its models, or leave blank to follow your default."
                }
              >
                {draft.model_provider ? (
                  <ModelPicker
                    provider={draft.model_provider}
                    value={draft.model ?? ""}
                    onChange={(model) => update({ model: model || null })}
                    clearOnProviderChange={false}
                  />
                ) : (
                  <input
                    className="mono"
                    style={{ fontSize: 12 }}
                    value={draft.model ?? ""}
                    placeholder="follows your default"
                    onChange={(e) => update({ model: e.target.value || null })}
                  />
                )}
              </Field>
              <Field
                label="Settings"
                top
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
                <pre className="source">{detail.source_toml}</pre>
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
