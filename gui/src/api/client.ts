/**
 * Talks to the local vikram-api.
 *
 * The base URL and bearer token come from the Rust shell, which generated the
 * token and read the port off the server's handshake line. Neither is known at
 * build time, so every call goes through `configure` first.
 */
import { invoke } from "@tauri-apps/api/core";

export interface ApiConfig {
  base_url: string;
  token: string;
  /** Directory `vikram gui` was launched from; null for a Finder launch. */
  launch_dir: string | null;
}

export interface SidecarError {
  kind: string;
  message: string;
  hint: string | null;
}

let config: ApiConfig | null = null;

function devConfigFromUrl(): ApiConfig | null {
  // Dev builds only, so a shipped app can never be handed a token by URL.
  if (!import.meta.env.DEV) return null;
  const params = new URLSearchParams(window.location.search);
  const base_url = params.get("api");
  const token = params.get("token");
  return base_url && token ? { base_url, token, launch_dir: null } : null;
}

export async function connect(): Promise<ApiConfig> {
  if (config) return config;
  // Running in a plain browser rather than the Tauri shell: useful for
  // frontend work, where Chrome devtools and hot reload beat the webview.
  const fromUrl = devConfigFromUrl();
  if (fromUrl) {
    config = fromUrl;
    return config;
  }
  config = await invoke<ApiConfig>("api_config");
  return config;
}

export function currentConfig(): ApiConfig {
  if (!config) throw new Error("API is not connected yet.");
  return config;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const { base_url, token } = await connect();
  const response = await fetch(base_url + path, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload?.detail) detail = JSON.stringify(payload.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T,>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T,>(path: string) => request<T>("DELETE", path),
};

// --- shapes the screens use -------------------------------------------

export interface AgentSummary {
  id: string;
  name: string;
  description: string;
  root: "user" | "builtin";
  writable: boolean;
  cli_only: boolean;
  tools: string[];
  resolved_provider: string | null;
  resolved_model: string | null;
  mcp_server_count: number;
  hook_count: number;
  shadows: string | null;
  error: string | null;
}

export interface Diagnostic {
  name: string;
  status: "ok" | "warning" | "error";
  detail: string;
  fix: string | null;
}

export type SessionState = "needs" | "running" | "idle";

export interface PendingApproval {
  approval_id: string;
  tool_name: string;
  input: Record<string, unknown>;
}

export interface SessionInfo {
  session_id: string;
  agent_id: string;
  agent_name: string;
  workspace: string;
  model: string | null;
  closed: boolean;
  state: SessionState;
  turns: number;
  activity: string;
  pending_approval: PendingApproval | null;
  started_at: number;
  last_event_at: number;
  elapsed_ms: number;
  tool_names: string[];
  approval_tool_names: string[];
  name?: string;
  model_config?: { provider: string; model: string };
}

export const listAgents = () =>
  api.get<{ agents: AgentSummary[] }>("/v1/agents").then((r) => r.agents);

export const runDoctor = (agentId?: string) =>
  api
    .get<{ diagnostics: Diagnostic[] }>(
      "/v1/doctor" + (agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""),
    )
    .then((r) => r.diagnostics);

export const listSessions = () =>
  api.get<{ sessions: SessionInfo[]; waiting: number; total: number }>(
    "/v1/sessions",
  );

export const openSession = (agentId: string, workspace: string) =>
  api.post<SessionInfo>("/v1/sessions", {
    agent_id: agentId,
    workspace,
  });

export const closeSession = (sessionId: string) =>
  api.del<void>(`/v1/sessions/${sessionId}`);

export const sendPrompt = (sessionId: string, prompt: string) =>
  api.post<{ turn_id: string }>(`/v1/sessions/${sessionId}/messages`, {
    prompt,
  });

export const answerApproval = (
  sessionId: string,
  approvalId: string,
  decision: "allow" | "deny" | "allow_always",
  toolName?: string,
) =>
  api.post<void>(`/v1/sessions/${sessionId}/approvals/${approvalId}`, {
    decision,
    tool_name: toolName ?? null,
  });

export const cancelTurn = (sessionId: string) =>
  api.post<void>(`/v1/sessions/${sessionId}/cancel`);

// --- editor, playground, settings --------------------------------------

export interface ToolInfo {
  name: string;
  description: string;
  requires_approval: boolean;
  sequential: boolean;
  /** Absent on servers predating the three-valued field. */
  approval?: "always" | "policy" | "never";
}

export interface ProviderInfo {
  id: string;
  display_name: string;
  needs_api_key: boolean;
  api_key_env: string | null;
  prompt_base_url: boolean;
  base_url_hint: string | null;
  default_base_url: string | null;
  suggested_model: string | null;
  configured_model: string | null;
  has_credential: boolean;
  base_url: string | null;
}

export interface ModelOption {
  id: string;
  label: string;
  meta: string;
}

/** Why a listing failed matters as much as the list: the field falls back
 *  to free text, and the reason is what tells you which one to type. */
export interface ModelListing {
  provider: string;
  ok: boolean;
  models: ModelOption[];
  error: string | null;
  source: string | null;
  enumerable: boolean;
  fetched_at: number;
}

export interface ValidationIssue {
  field: string | null;
  severity: "error" | "warning";
  message: string;
  fix: string | null;
}

export interface ValidationReport {
  ok: boolean;
  issues: ValidationIssue[];
  system_prompt: string | null;
  tool_names: string[];
  approval_tool_names: string[];
  model_config: Record<string, unknown> | null;
}

export interface AgentDetail {
  summary: AgentSummary;
  draft: Record<string, any>;
  system_prompt: string;
  source_toml: string;
  path: string;
}

export interface ConfigView {
  path: string;
  default_provider: string | null;
  top_level_model: string | null;
  providers: Record<
    string,
    { model: string | null; base_url: string | null; has_api_key: boolean }
  >;
  agents: Record<string, { provider: string; model: string }>;
}

export interface ColumnMetrics {
  column_id: string;
  provider: string;
  model: string;
  ttft_ms: number | null;
  total_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  tool_calls: number;
  output: string;
  error: string | null;
}

export const getAgent = (id: string) => api.get<AgentDetail>(`/v1/agents/${id}`);

export const saveAgent = (
  id: string,
  draft: Record<string, any>,
  systemPrompt: string,
) =>
  api.put<AgentDetail>(`/v1/agents/${id}`, {
    draft,
    system_prompt: systemPrompt,
  });

export const validateDraft = (draft: Record<string, any>, agentId?: string) =>
  api.post<ValidationReport>("/v1/agents/validate", {
    draft,
    agent_id: agentId ?? null,
  });

export const listTools = () =>
  api.get<{ tools: ToolInfo[] }>("/v1/tools").then((r) => r.tools);

export const listModels = (providerId: string, refresh = false) =>
  api.get<ModelListing>(
    `/v1/providers/${encodeURIComponent(providerId)}/models` +
      (refresh ? "?refresh=true" : ""),
  );

export const createAgent = (
  id: string,
  draft: Record<string, any>,
  systemPrompt: string,
) =>
  api.post<AgentDetail>("/v1/agents", {
    id,
    draft,
    system_prompt: systemPrompt,
  });

/** The TOML tab is the same draft, rendered. Round-tripping through the
 *  server keeps one serializer rather than a second one in the GUI. */
export const renderToml = (draft: Record<string, any>, existing?: string) =>
  api
    .post<{ toml: string }>("/v1/agents/render-toml", {
      draft,
      existing: existing ?? null,
    })
    .then((r) => r.toml);

export const parseToml = (toml: string) =>
  api
    .post<{ draft: Record<string, any> }>("/v1/agents/parse-toml", { toml })
    .then((r) => r.draft);

export const listProviders = () =>
  api
    .get<{ providers: ProviderInfo[] }>("/v1/providers")
    .then((r) => r.providers);

export const testMcp = (server: Record<string, unknown>) =>
  api.post<{ ok: boolean; error: string | null; tools: string[] }>(
    "/v1/mcp/test",
    server,
  );

export const getConfig = () => api.get<ConfigView>("/v1/config");

export const saveProvider = (id: string, values: Record<string, string>) =>
  api.put<ConfigView>(`/v1/config/providers/${id}`, values);

export const setDefaultProvider = (provider: string) =>
  api.put<ConfigView>("/v1/config/default-provider", { provider });

export const startComparison = (
  agentId: string,
  workspace: string,
  prompt: string,
  columns: { provider: string; model: string }[],
) =>
  api.post<SessionInfo & { turn_id: string }>("/v1/playground/runs", {
    agent_id: agentId,
    workspace,
    prompt,
    columns,
  });
