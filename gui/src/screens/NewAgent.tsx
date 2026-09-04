import { useEffect, useMemo, useState } from "react";
import {
  ProviderInfo,
  ToolInfo,
  ValidationReport,
  createAgent,
  listProviders,
  listTools,
  parseToml,
  renderToml,
  validateDraft,
} from "../api/client";
import { McpEditor } from "../components/McpEditor";
import { ModelPicker } from "../components/ModelPicker";
import { ToolList } from "../components/ToolList";
import { Eyebrow, Field, Fields } from "../components/primitives";

/**
 * Make a new agent.
 *
 * One page rather than a section rail: the whole draft is a single scroll,
 * under a bar that does not move, so Create is reachable from anywhere in it.
 * The TOML tab is the same draft rendered, not a second document -- the
 * server owns the serializer either way, so what you see is what gets
 * written.
 *
 * Nothing touches the filesystem until Create; the backing endpoint is
 * POST /v1/agents, which has existed since the store landed.
 */
interface Props {
  onCancel: () => void;
  onCreated: (agentId: string) => void;
}

type Tab = "form" | "toml";

const BLANK: Record<string, any> = {
  name: "",
  description: "",
  system_prompt: "system_prompt.md",
  cli_only: false,
  context_files: [],
  skills: [],
  shared_context_files: [],
  shared_skills: [],
  tools: [],
  mcp_servers: [],
  hooks: [],
  model_provider: null,
  model: null,
  model_settings: {},
};

/** Mirrors validate_agent_id in specstore.py: lowercase, digits, - and _. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export function NewAgent({ onCancel, onCreated }: Props) {
  const [draft, setDraft] = useState<Record<string, any>>(BLANK);
  const [prompt, setPrompt] = useState("");
  const [agentId, setAgentId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [tab, setTab] = useState<Tab>("form");
  const [toml, setToml] = useState("");
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listTools().then(setTools).catch((e) => setError(String(e)));
    listProviders().then(setProviders).catch((e) => setError(String(e)));
  }, []);

  const id = idTouched ? agentId : slugify(draft.name ?? "");
  const provider = draft.model_provider ?? "";

  const update = (patch: Record<string, any>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    setReport(null);
    setStatus(null);
  };

  /** Switching tabs carries the draft through the server's own serializer,
   *  so neither view can quietly hold something the other cannot express. */
  async function switchTo(next: Tab) {
    if (next === tab) return;
    setError(null);
    try {
      if (next === "toml") {
        setToml(await renderToml(draft, toml || undefined));
      } else {
        setDraft(await parseToml(toml));
        setReport(null);
      }
      setTab(next);
    } catch (e) {
      setError(String(e));
    }
  }

  /** In the TOML tab the text is the truth; parse before doing anything. */
  async function current(): Promise<Record<string, any>> {
    return tab === "toml" ? await parseToml(toml) : draft;
  }

  async function validate() {
    setBusy(true);
    setStatus("Validating…");
    setError(null);
    try {
      const result = await validateDraft(await current(), id || undefined);
      setReport(result);
      setStatus(result.ok ? "Valid" : null);
    } catch (e) {
      setError(String(e));
      setStatus(null);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    if (!id) return setError("Give the agent a name first.");
    setBusy(true);
    setStatus("Creating…");
    setError(null);
    try {
      await createAgent(id, await current(), prompt);
      onCreated(id);
    } catch (e) {
      setError(String(e));
      setStatus(null);
    } finally {
      setBusy(false);
    }
  }

  const issues = report?.issues ?? [];
  const warnings = useMemo(
    () => (prompt.trim() ? 0 : 1) + issues.filter((i) => i.severity === "error").length,
    [prompt, issues],
  );

  return (
    <div className="editor">
      <header className="session-head">
        <div className="row" style={{ gap: 12 }}>
          <strong>New agent</strong>
          <span className="path">
            {id ? `~/.config/vikram/agents/${id}/` : "unnamed"}
          </span>
          <span className="divider" />
          <span className="filters">
            <button
              className={`eyebrow${tab === "form" ? " active" : ""}`}
              onClick={() => switchTo("form")}
            >
              Form
            </button>
            <button
              className={`eyebrow${tab === "toml" ? " active" : ""}`}
              onClick={() => switchTo("toml")}
            >
              TOML
            </button>
          </span>
        </div>
        <div className="row">
          {status && <Eyebrow>{status}</Eyebrow>}
          {!status && warnings > 0 && (
            <Eyebrow className="accent">
              {warnings === 1 ? "1 warning" : `${warnings} warnings`}
            </Eyebrow>
          )}
          <button className="btn quiet" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn secondary" disabled={busy} onClick={validate}>
            Validate
          </button>
          <button className="btn" disabled={busy || !id} onClick={create}>
            Create agent
          </button>
        </div>
      </header>

      <div className="pane">
        {tab === "toml" ? (
          <Fields wide>
            <Field
              label="Agent id"
              hint="The folder name is not part of the file, so it stays a field in both modes."
            >
              <input
                className="mono"
                style={{ fontSize: 12, maxWidth: "40ch" }}
                value={id}
                onChange={(e) => {
                  setIdTouched(true);
                  setAgentId(e.target.value);
                }}
              />
            </Field>
            <Field
              label="agent.toml"
              top
              hint="The same draft as the form — switching tabs carries whichever you edited last, and comments survive the round trip."
            >
              <textarea
                className="code"
                rows={22}
                value={toml}
                onChange={(e) => setToml(e.target.value)}
              />
            </Field>
          </Fields>
        ) : (
          <>
            <Section label="Identity">
              <Fields>
                <Field label="Name">
                  <input
                    value={draft.name ?? ""}
                    placeholder="Release notes"
                    onChange={(e) => update({ name: e.target.value })}
                  />
                </Field>
                <Field
                  label="Agent id"
                  hint={
                    <>
                      Folder name under{" "}
                      <span className="mono">~/.config/vikram/agents</span>. Follows
                      the name until you type your own.
                    </>
                  }
                >
                  <input
                    className="mono"
                    style={{ fontSize: 12 }}
                    value={id}
                    placeholder="release-notes"
                    onChange={(e) => {
                      setIdTouched(true);
                      setAgentId(e.target.value);
                    }}
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
            </Section>

            <Section label="Model">
              <Fields>
                <Field
                  label="Provider"
                  hint="Leave blank to follow your default. Changing it clears the model and refetches."
                >
                  <div className="row" style={{ gap: 8 }}>
                    <span className={`dot${ready(providers, provider) ? " live" : ""}`} />
                    <select
                      value={provider}
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
                  </div>
                </Field>
                <Field
                  label="Model"
                  hint={
                    provider
                      ? "Read from the provider above. A name you type by hand is still accepted."
                      : "Pick a provider to list its models, or leave blank to follow your default."
                  }
                >
                  {provider ? (
                    <ModelPicker
                      provider={provider}
                      value={draft.model ?? ""}
                      onChange={(model) => update({ model: model || null })}
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
                    rows={5}
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
            </Section>

            <Section
              label="System prompt"
              right={
                !prompt.trim() && <Eyebrow className="accent">1 warning</Eyebrow>
              }
            >
              <Fields wide>
                <Field
                  label="Prompt"
                  top
                  hint={
                    <>
                      Written to <span className="mono">{draft.system_prompt}</span>{" "}
                      beside the spec. Shared context files are appended at load time.
                    </>
                  }
                >
                  <textarea
                    className="code"
                    rows={9}
                    value={prompt}
                    placeholder="What this agent is for, how it should behave, and what it must not do…"
                    onChange={(e) => setPrompt(e.target.value)}
                  />
                </Field>
              </Fields>
              {!prompt.trim() && (
                <div className="issues">
                  <p className="warn">
                    <strong>system_prompt</strong>: empty — the agent would start with
                    no instructions.{" "}
                    <span className="muted">
                      — Write one before creating, or after.
                    </span>
                  </p>
                </div>
              )}
            </Section>

            <Section
              label="Tools"
              right={
                <Eyebrow>
                  {(draft.tools ?? []).length} of {tools.length} selected
                </Eyebrow>
              }
            >
              <ToolList
                tools={tools}
                selected={draft.tools ?? []}
                onChange={(next) => update({ tools: next })}
              />
            </Section>

            <Section
              label="MCP servers"
              right={<Eyebrow>{(draft.mcp_servers ?? []).length}</Eyebrow>}
            >
              <McpEditor
                servers={draft.mcp_servers ?? []}
                onChange={(mcp_servers) => update({ mcp_servers })}
              />
            </Section>
          </>
        )}

        {error && <p className="error" style={{ marginTop: 14 }}>{error}</p>}

        {issues.length > 0 && (
          <div className="issues">
            {issues.map((issue, i) => (
              <p key={i} className={issue.severity === "error" ? "error" : "warn"}>
                <strong>{issue.field ?? "spec"}</strong>: {issue.message}
                {issue.fix && <span className="muted"> — {issue.fix}</span>}
              </p>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** A provider is usable when it needs no key, or already has one. */
function ready(providers: ProviderInfo[], id: string): boolean {
  const provider = providers.find((p) => p.id === id);
  if (!provider) return false;
  return !provider.needs_api_key || provider.has_credential;
}

function Section({
  label,
  right,
  children,
}: {
  label: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="sec">
      <div className="sec-head">
        <Eyebrow className="grow">{label}</Eyebrow>
        {right}
      </div>
      {children}
    </section>
  );
}
