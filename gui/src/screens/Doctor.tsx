import { useEffect, useState } from "react";
import { Diagnostic, runDoctor } from "../api/client";

export function Doctor() {
  const [items, setItems] = useState<Diagnostic[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setItems(null);
    runDoctor().then(setItems).catch((e) => setError(String(e)));
  }

  useEffect(refresh, []);

  if (error) return <p className="error">{error}</p>;

  return (
    <div className="stack">
      <header className="screen-head">
        <h1>Doctor</h1>
        <p className="muted">The same checks as <code>vikram doctor</code>.</p>
      </header>
      <div><button onClick={refresh}>Re-run</button></div>
      {!items ? (
        <p className="muted">Checking…</p>
      ) : (
        <table className="diagnostics">
          <tbody>
            {items.map((item) => (
              <tr key={item.name}>
                <td className={`status status-${item.status}`}>
                  {item.status === "ok" ? "✓" : item.status === "warning" ? "!" : "✗"}
                </td>
                <td className="name">{item.name}</td>
                <td>
                  <div>{item.detail}</div>
                  {item.fix && <div className="muted small">{item.fix}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
