import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import {
  AgentSummary,
  currentConfig,
  listAgents,
  openSession,
} from "../api/client";
import { Eyebrow } from "../components/primitives";
import { basename, recentWorkspaces, rememberWorkspace } from "../recent";

interface Props {
  /** Preselected when arriving from an agent row; null from "+ New session". */
  agent: AgentSummary | null;
  onCancel: () => void;
  onStarted: (sessionId: string) => void;
}

/** Folders offered without opening a dialog, most likely first.
 *
 * The picker used to be the only way through, which made every session start
 * with a modal and a navigation. In practice people work in a handful of
 * repositories, so the folder is nearly always one they have used before — or,
 * on a first run, the one they were standing in when they typed `vikram gui`.
 */
function defaultChoices(launchDir: string | null): string[] {
  const recents = recentWorkspaces();
  if (launchDir && !recents.includes(launchDir)) return [launchDir, ...recents];
  return recents;
}

export function NewSession({ agent, onCancel, onStarted }: Props) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentId, setAgentId] = useState(agent?.id ?? "");
  const [choices, setChoices] = useState<string[]>([]);
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const offered = defaultChoices(currentConfig().launch_dir);
    setChoices(offered);
    // Preselecting is the whole point: Start is reachable in one click.
    if (offered[0]) setWorkspace(offered[0]);

    listAgents()
      .then((list) => {
        setAgents(list);
        if (!agentId && list[0]) setAgentId(list[0].id);
      })
      .catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function choose() {
    const chosen = await open({ directory: true, multiple: false });
    if (typeof chosen !== "string") return;
    setChoices((prev) => [chosen, ...prev.filter((p) => p !== chosen)]);
    setWorkspace(chosen);
  }

  async function start() {
    if (!agentId || !workspace) return;
    setStarting(true);
    setError(null);
    try {
      const info = await openSession(agentId, workspace);
      rememberWorkspace(workspace);
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
      </div>

      <div className="folders">
        <div className="folders-head">
          <Eyebrow className="grow">Folder</Eyebrow>
          <button className="eyebrow link-quiet" onClick={choose}>
            Browse…
          </button>
        </div>

        {choices.length === 0 && (
          <p className="muted small" style={{ padding: "8px 0" }}>
            No folders yet — pick one with Browse.
          </p>
        )}

        {choices.map((path) => (
          <button
            key={path}
            className={`folder-row${path === workspace ? " active" : ""}`}
            onClick={() => setWorkspace(path)}
            title={path}
          >
            <span className="mark" />
            {/* The folder name is what tells two rows apart; the path is
                there to disambiguate two folders sharing a name. */}
            <span className="folder-name grow">{basename(path)}</span>
            <span className="path">{path}</span>
          </button>
        ))}
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <button
          className="btn"
          disabled={!workspace || !agentId || starting}
          onClick={start}
        >
          {starting ? "Starting…" : "Start session"}
        </button>
        <button className="btn quiet" onClick={onCancel}>
          Cancel
        </button>
      </div>

      {error && (
        <p className="error" style={{ marginTop: 12 }}>
          {error}
        </p>
      )}
    </div>
  );
}
