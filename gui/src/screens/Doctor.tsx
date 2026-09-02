import { useEffect, useState } from "react";
import { Diagnostic, runDoctor } from "../api/client";
import { Rule } from "../components/primitives";

const MARK: Record<Diagnostic["status"], string> = { ok: "✓", warning: "!", error: "✗" };

export function Doctor() {
  const [items, setItems] = useState<Diagnostic[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setItems(null);
    runDoctor()
      .then(setItems)
      .catch((e) => setError(String(e)));
  }

  useEffect(refresh, []);

  if (error) return <p className="error">{error}</p>;

  return (
    <div className="screen">
      <div className="head-row">
        <div>
          <h1 className="page">Doctor</h1>
          <p className="lede">
            The same checks as <span className="mono">vikram doctor</span>, run against
            this machine.
          </p>
        </div>
        <button className="btn secondary" onClick={refresh}>
          Re-run
        </button>
      </div>

      <div className="rule" />
      <Rule />

      {!items ? (
        <p className="muted" style={{ paddingTop: 22 }}>Checking…</p>
      ) : (
        <table className="diagnostics">
          <tbody>
            {items.map((item) => (
              <tr key={item.name}>
                <td className={`mark ${item.status}`}>{MARK[item.status]}</td>
                <td className="name">
                  <span className="eyebrow">{item.name}</span>
                </td>
                <td>
                  <div className="mono small">{item.detail}</div>
                  {item.fix && (
                    <div className="muted small" style={{ marginTop: 4 }}>{item.fix}</div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
