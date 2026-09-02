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
}

export interface SidecarError {
  kind: string;
  message: string;
  hint: string | null;
}

let config: ApiConfig | null = null;

export async function connect(): Promise<ApiConfig> {
  if (config) return config;
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

export interface SessionInfo {
  session_id: string;
  agent_id: string;
  workspace: string;
  closed: boolean;
  name?: string;
  model_config?: { provider: string; model: string };
  tool_names?: string[];
  approval_tool_names?: string[];
}

export const listAgents = () =>
  api.get<{ agents: AgentSummary[] }>("/v1/agents").then((r) => r.agents);

export const runDoctor = (agentId?: string) =>
  api
    .get<{ diagnostics: Diagnostic[] }>(
      "/v1/doctor" + (agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""),
    )
    .then((r) => r.diagnostics);

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
