import { useEffect, useState } from "react";
import { ConfigView, ProviderInfo, getConfig, listProviders, saveProvider, setDefaultProvider } from "../api/client";

export function Settings() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    Promise.all([listProviders(), getConfig()])
      .then(([p, c]) => { setProviders(p); setConfig(c); })
      .catch((e) => setError(String(e)));
  }
  useEffect(refresh, []);

  if (error) return <p className="error">{error}</p>;
  if (!config) return <p className="muted">Loading…</p>;

  async function save(id: string, values: Record<string, string>) {
    setStatus("Saving…");
    try {
      await saveProvider(id, values);
      setStatus("Saved");
      refresh();
    } catch (e) { setError(String(e)); setStatus(null); }
  }

  return (
    <div className="stack">
      <header className="screen-head">
        <h1>Settings</h1>
        <p className="muted">
          Written to <code>{config.path}</code>. API keys are stored there and
          never sent back to this window.
        </p>
      </header>

      {config.top_level_model && (
        <p className="warn">
          A top-level <code>model = "{config.top_level_model}"</code> in your
          config overrides every agent's own model. Remove it by hand unless you
          meant it.
        </p>
      )}

      <div className="row">
        <span className="muted">Default provider</span>
        <select
          value={config.default_provider ?? ""}
          onChange={async (e) => { await setDefaultProvider(e.target.value); refresh(); }}
        >
          {providers.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
        </select>
        {status && <span className="muted small">{status}</span>}
      </div>

      <div className="cards">
        {providers.map((provider) => (
          <ProviderCard key={provider.id} provider={provider} onSave={save} />
        ))}
      </div>
    </div>
  );
}

function ProviderCard({
  provider,
  onSave,
}: {
  provider: ProviderInfo;
  onSave: (id: string, values: Record<string, string>) => void;
}) {
  const [model, setModel] = useState(provider.configured_model ?? "");
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [apiKey, setApiKey] = useState("");

  return (
    <article className="card">
      <div className="card-head">
        <h2>{provider.display_name}</h2>
        {provider.needs_api_key && (
          <span className={`badge ${provider.has_credential ? "badge-user" : ""}`}>
            {provider.has_credential ? "key set" : "no key"}
          </span>
        )}
      </div>
      <label className="field">
        <span className="field-label">Model</span>
        <input value={model} placeholder={provider.suggested_model ?? ""} onChange={(e) => setModel(e.target.value)} />
      </label>
      {provider.prompt_base_url && (
        <label className="field">
          <span className="field-label">Base URL</span>
          <input value={baseUrl} placeholder={provider.default_base_url ?? ""} onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
      )}
      {provider.needs_api_key && (
        <label className="field">
          <span className="field-label">API key</span>
          <span className="muted small">
            Leave blank to keep the stored key. Env: <code>{provider.api_key_env}</code>
          </span>
          <input type="password" value={apiKey} placeholder="••••••" onChange={(e) => setApiKey(e.target.value)} />
        </label>
      )}
      <div className="card-actions">
        <button
          className="secondary"
          onClick={() => {
            const values: Record<string, string> = {};
            if (model) values.model = model;
            if (provider.prompt_base_url && baseUrl) values.base_url = baseUrl;
            if (apiKey) values.api_key = apiKey;
            onSave(provider.id, values);
            setApiKey("");
          }}
        >
          Save
        </button>
      </div>
    </article>
  );
}
