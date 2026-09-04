import { useEffect, useMemo, useRef, useState } from "react";
import { ModelListing, listModels } from "../api/client";

/**
 * A model field backed by the provider's own list.
 *
 * The list is a shortcut, never a gate. Every failure path below still
 * leaves a plain text field holding whatever you type, because that is what
 * `agent.toml` stores and a name the provider has not heard of yet is a
 * legitimate thing to write.
 *
 * The listing is keyed to `provider`: changing it refetches, and the old
 * provider's model is not carried over.
 */
interface Props {
  provider: string;
  value: string;
  onChange: (model: string) => void;
  placeholder?: string;
  /** Cleared when the provider changes. Off for the editor, where a blank
   *  model means "follow the default" and must survive a provider switch. */
  clearOnProviderChange?: boolean;
}

export function ModelPicker({
  provider,
  value,
  onChange,
  placeholder = "Choose a model…",
  clearOnProviderChange = true,
}: Props) {
  const [listing, setListing] = useState<ModelListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const previousProvider = useRef(provider);

  async function load(refresh = false) {
    if (!provider) return setListing(null);
    setLoading(true);
    try {
      setListing(await listModels(provider, refresh));
    } catch (e) {
      // A transport failure reads the same as a provider failure here: the
      // field falls back to free text either way.
      setListing({
        provider,
        ok: false,
        models: [],
        error: String(e),
        source: null,
        enumerable: true,
        fetched_at: 0,
      });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (clearOnProviderChange && previousProvider.current !== provider) {
      onChange("");
      setQuery("");
    }
    previousProvider.current = provider;
    load();
    // onChange is not a dependency: it is recreated every render by callers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  useEffect(() => {
    if (!open) return;
    function away(event: MouseEvent) {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const matches = useMemo(() => {
    const models = listing?.models ?? [];
    const needle = query.trim().toLowerCase();
    if (!needle) return models;
    return models.filter(
      (m) =>
        m.id.toLowerCase().includes(needle) ||
        m.label.toLowerCase().includes(needle),
    );
  }, [listing, query]);

  // Nothing to pick from: fall back to the plain field this replaced, and
  // say why, so you know which name to type.
  if (!loading && listing && !listing.ok) {
    return (
      <div className="picker">
        <input
          className="mono"
          style={{ fontSize: 12 }}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
        <p className={`small ${listing.enumerable ? "warn" : "muted"}`}
           style={{ margin: "6px 0 0" }}>
          {listing.error}
          {listing.enumerable && (
            <button
              className="btn quiet"
              style={{ marginLeft: 8, textDecoration: "underline" }}
              onClick={() => load(true)}
            >
              Retry
            </button>
          )}
        </p>
      </div>
    );
  }

  return (
    <div className="picker" ref={box}>
      <button
        type="button"
        className={`picker-trigger${open ? " open" : ""}`}
        onClick={() => setOpen(!open)}
      >
        <span className={`val${value ? "" : " placeholder"}`}>
          {loading && !value ? "Reading models…" : value || placeholder}
        </span>
        <span className="chev">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="1.8">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="sheet">
          <div className="sheet-search">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="1.8">
              <circle cx="11" cy="11" r="7" />
              <path d="M20 20l-3.5-3.5" />
            </svg>
            <input
              autoFocus
              value={query}
              placeholder={
                loading
                  ? "Reading models…"
                  : `Filter ${listing?.models.length ?? 0} models…`
              }
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>

          {loading ? (
            <div style={{ padding: "11px 9px", display: "flex",
                          flexDirection: "column", gap: 11 }}>
              <div className="skeleton" style={{ width: "62%" }} />
              <div className="skeleton" style={{ width: "44%" }} />
              <div className="skeleton" style={{ width: "53%" }} />
            </div>
          ) : (
            <div className="sheet-list">
              <div className="eyebrow rail-group">Available · {provider}</div>
              {matches.map((model) => (
                <button
                  key={model.id}
                  type="button"
                  className={`opt${model.id === value ? " on" : ""}`}
                  onClick={() => {
                    onChange(model.id);
                    setOpen(false);
                  }}
                >
                  <span className={`dot${model.id === value ? " live" : ""}`}
                        style={model.id === value ? undefined
                                                  : { background: "transparent" }} />
                  <span className="name">{model.label}</span>
                  <span className="meta">{model.meta}</span>
                </button>
              ))}

              {/* Never a dead end: the typed name is still writable. */}
              {matches.length === 0 && query.trim() && (
                <button
                  type="button"
                  className="opt"
                  style={{ height: "auto", padding: "8px 9px", color: "var(--fg)" }}
                  onClick={() => {
                    onChange(query.trim());
                    setOpen(false);
                  }}
                >
                  <span style={{ fontSize: 12.5 }}>
                    Use <span className="mono" style={{ fontSize: 12 }}>
                      {query.trim()}
                    </span> anyway
                  </span>
                </button>
              )}
              {matches.length === 0 && !query.trim() && (
                <p className="muted small" style={{ padding: "8px 9px", margin: 0 }}>
                  The provider listed no models.
                </p>
              )}
            </div>
          )}

          <div className="sheet-foot">
            <button
              type="button"
              className="btn quiet"
              style={{ display: "flex", alignItems: "center", gap: 6 }}
              onClick={() => load(true)}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="1.8">
                <path d="M20 11a8 8 0 1 0-2.3 5.7" />
                <path d="M20 5v6h-6" />
              </svg>
              Refresh
            </button>
            <span className="grow" />
            <span className="eyebrow">{age(listing)}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function age(listing: ModelListing | null): string {
  if (!listing?.fetched_at) return "";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - listing.fetched_at));
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}
