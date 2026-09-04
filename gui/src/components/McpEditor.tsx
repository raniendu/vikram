import { useState } from "react";
import { testMcp } from "../api/client";

/** Shared by the editor and the create screen, so the two cannot drift. */
export function McpEditor({
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
    <div className="fields" style={{ maxWidth: "none" }}>
      {servers.length === 0 && (
        <p className="muted small" style={{ margin: 0 }}>
          None yet. Attach a Model Context Protocol server to give this agent
          tools Vikram does not ship.
        </p>
      )}

      {servers.map((server, i) => (
        <div key={i} className="mcp-entry">
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
            <button className="btn secondary" onClick={() => test(i)}>
              Test
            </button>
            <button
              className="btn quiet"
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
          className="btn secondary"
          onClick={() =>
            onChange([...servers, { name: "", transport: "stdio", command: "" }])
          }
        >
          Add server
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        Reference secrets as <code>${"{ENV_VAR}"}</code>; they are never stored in
        the spec or returned by the API.
      </p>
    </div>
  );
}
