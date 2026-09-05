import { useEffect, useState } from "react";
import {
  ConfigView,
  ProviderInfo,
  getConfig,
  listProviders,
  saveProvider,
  setDefaultProvider,
} from "../api/client";
import { ModelPicker } from "../components/ModelPicker";
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
  onSave: (values: Record<string, string>) => Promise<void>;
}) {
  const [model, setModel] = useState(provider.configured_model ?? "");
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  // Saving a key is what makes a model list possible, so the picker has to
  // go and look again rather than keep showing the reason it could not.
  const [listing, setListing] = useState(0);
  const dirty =
    model !== (provider.configured_model ?? "") ||
    baseUrl !== (provider.base_url ?? "") ||
    apiKey !== "";

  return (
    <div className="list-row providers">
      <div>
        <div style={{ fontSize: 16.8, fontWeight: 500 }}>{provider.display_name}</div>
        {provider.api_key_env && (
          <Eyebrow style={{ marginTop: 5 }}>{provider.api_key_env}</Eyebrow>
        )}
      </div>

      {/* Key first: a keyed provider cannot list a single model until it has
          one, so asking for the model first asks you to name something the
          app has no way to check. */}
      <div className="row" style={{ gap: 8, alignItems: "start" }}>
        {provider.needs_api_key && (
          <div style={{ flex: "1 1 170px" }}>
            <input
              type="password"
              value={apiKey}
              placeholder="Leave blank to keep the stored key"
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>
        )}
        {provider.prompt_base_url && (
          <div style={{ flex: "1 1 180px" }}>
            <input
              className="mono"
              style={{ fontSize: 12 }}
              value={baseUrl}
              placeholder={provider.default_base_url ?? "base URL"}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
        )}
        <div style={{ flex: "1 1 210px" }}>
          <ModelPicker
            key={listing}
            provider={provider.id}
            value={model}
            onChange={setModel}
            placeholder={provider.suggested_model ?? "Choose a model…"}
            clearOnProviderChange={false}
          />
        </div>
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
          onClick={async () => {
            const values: Record<string, string> = {};
            if (model) values.model = model;
            if (provider.prompt_base_url && baseUrl) values.base_url = baseUrl;
            if (apiKey) values.api_key = apiKey;
            const credentialChanged =
              !!apiKey || baseUrl !== (provider.base_url ?? "");
            await onSave(values);
            setApiKey("");
            // Only after the write lands -- refetching first would ask with
            // the credentials the server had a moment ago.
            if (credentialChanged) setListing((n) => n + 1);
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}
