import { ToolInfo } from "../api/client";

/** One line per tool: on/off, name, what it does, whether it stops for you. */
export function ToolList({
  tools,
  selected,
  onChange,
}: {
  tools: ToolInfo[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  function toggle(name: string, on: boolean) {
    onChange(on ? [...selected, name] : selected.filter((t) => t !== name));
  }

  return (
    <div>
      {tools.map((tool) => {
        const on = selected.includes(tool.name);
        return (
          <label key={tool.name} className="tool-row">
            <input
              type="checkbox"
              checked={on}
              style={{ display: "none" }}
              onChange={(e) => toggle(tool.name, e.target.checked)}
            />
            <span className={`check${on ? " on" : ""}`}>{on ? "✓" : ""}</span>
            <span className="mono" style={{ fontSize: 11.5 }}>{tool.name}</span>
            <span className="desc">{tool.description}</span>
            <ApprovalFlag tool={tool} />
          </label>
        );
      })}
    </div>
  );
}

function ApprovalFlag({ tool }: { tool: ToolInfo }) {
  // Falls back to the older boolean so a server predating the three-valued
  // field still shows the flag rather than silently dropping a
  // security-relevant one.
  const approval = tool.approval ?? (tool.requires_approval ? "always" : "never");
  if (approval === "always")
    return (
      <span className="eyebrow accent flag" style={{ fontSize: 9 }}>
        needs approval
      </span>
    );
  if (approval === "policy")
    return (
      <span
        className="eyebrow accent flag"
        style={{ fontSize: 9 }}
        title="Decided per call by the command policy"
      >
        by policy
      </span>
    );
  return <span className="flag" />;
}
