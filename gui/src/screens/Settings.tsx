import { useEffect, useState } from "react";
import {
  ConfigView,
  ProviderInfo,
  getConfig,
  listProviders,
  saveProvider,
  setDefaultProvider,
} from "../api/client";
import { Eyebrow, Rule } from "../components/primitives";

export function Settings() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    Promise.all([listProviders(), getConfig()])
      .then(([p, c]) => {
        setProviders(p);
        setConfig(c);
      })
      .catch((e) => setError(String(e)));
  }
  useEffect(refresh, []);

  if (error) return <p className="error">{error}</p>;
  if (!config) return <p className="muted">Loading…</p>;

  return (
    <div className="screen">
      <div className="head-row">
        <div>
          <h1 className="page">Settings</h1>
          <p className="lede">
            Written to <span className="mono">{config.path}</span>. Keys are stored
            there and never sent back to this window.
          </p>
        </div>
        <div className="row">
          {status && <Eyebrow>{status}</Eyebrow>}
          <select
            value={config.default_provider ?? ""}
            onChange={async (e) => {
              await setDefaultProvider(e.target.value);
              refresh();
            }}
          >
            {providers.map((p) => (
              <option key={p.id} value={p.id}>{p.display_name}</option>
            ))}
          </select>
        </div>
      </div>

      {config.top_level_model && (
        <p className="warn" style={{ marginTop: 18 }}>
          A top-level <span className="mono">model = "{config.top_level_model}"</span>{" "}
          in your config overrides every agent’s own model. Remove it by hand unless
          you meant it.
        </p>
      )}

      <div className="rule" />
      <Rule />

      {providers.map((provider) => (
        <ProviderRow
          key={provider.id}
          provider={provider}
          onSave={async (values) => {
            setStatus("Saving…");
            await saveProvider(provider.id, values);
            setStatus("Saved");
            refresh();
          }}
        />
      ))}
    </div>
  );
}

function ProviderRow({
  provider,
  onSave,
}: {
  provider: ProviderInfo;
  onSave: (values: Record<string, string>) => void;
}) {
  const [model, setModel] = useState(provider.configured_model ?? "");
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const dirty =
    model !== (provider.configured_model ?? "") ||
    baseUrl !== (provider.base_url ?? "") ||
    apiKey !== "";

  return (
    <div className="list-row" style={{ gridTemplateColumns: "232px minmax(0, 1fr) 150px" }}>
      <div>
        <div style={{ fontSize: 16.8, fontWeight: 500 }}>{provider.display_name}</div>
        {provider.api_key_env && (
          <Eyebrow style={{ marginTop: 5 }}>{provider.api_key_env}</Eyebrow>
        )}
      </div>

      <div className="fields" style={{ gap: 12 }}>
        <input
          value={model}
          placeholder={provider.suggested_model ?? "model"}
          onChange={(e) => setModel(e.target.value)}
        />
        {provider.prompt_base_url && (
          <input
            value={baseUrl}
            placeholder={provider.default_base_url ?? "base URL"}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        )}
        {provider.needs_api_key && (
          <input
            type="password"
            value={apiKey}
            placeholder="Leave blank to keep the stored key"
            onChange={(e) => setApiKey(e.target.value)}
          />
        )}
      </div>

      <div className="row-rail">
        {provider.needs_api_key && (
          <Eyebrow className={provider.has_credential ? "accent" : undefined}>
            {provider.has_credential ? "Key set" : "No key"}
          </Eyebrow>
        )}
        <button
          className="btn quiet"
          disabled={!dirty}
          onClick={() => {
            const values: Record<string, string> = {};
            if (model) values.model = model;
            if (provider.prompt_base_url && baseUrl) values.base_url = baseUrl;
            if (apiKey) values.api_key = apiKey;
            onSave(values);
            setApiKey("");
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}
