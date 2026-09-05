"""Ask each provider which models it will actually serve.

Every editing surface used to make you type a model name from memory, which
is how ``vikram doctor`` ends up reporting a model that was never installed.
This module asks the provider instead.

Three wire shapes cover the seven providers:

* Ollama exposes ``/api/tags``, which is the only source that also reports a
  size, so local models carry one.
* Anthropic and Gemini each have their own listing endpoint.
* Everything else speaks the OpenAI ``/v1/models`` shape.

A failure here is never fatal: a listing carries ``ok`` and ``error`` rather
than raising, because the caller's fallback is the plain text field that
existed before, and a name typed by hand stays valid whatever this returns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from vikram.logging import get_logger
from vikram.providers import PROVIDERS
from vikram.settings import VikramSettings

logger = get_logger(__name__)

TIMEOUT_SECONDS = 6.0
CACHE_TTL_SECONDS = 60.0

# Custom endpoints are the one case with nothing to ask: the base URL points
# at whatever the user is running, and /v1/models is optional in practice.
NOT_ENUMERABLE: frozenset[str] = frozenset({"openai-compatible"})


@dataclass(frozen=True)
class ModelOption:
    id: str
    """The name to write into ``agent.toml``."""

    label: str
    """What to show. The id, always: it is what lands in ``agent.toml``, so a
    friendlier name belongs in ``meta`` where it cannot be mistaken for the
    thing you are choosing."""

    meta: str = ""
    """One short right-aligned line: parameter size, or nothing."""


@dataclass
class ModelListing:
    provider: str
    ok: bool
    models: list[ModelOption] = field(default_factory=list)
    error: str | None = None
    source: str | None = None
    enumerable: bool = True
    fetched_at: float = 0.0
    """Unix seconds, so the GUI can show how stale the list is."""


_cache: dict[str, ModelListing] = {}


def _human_size(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{num_bytes / 1_000_000:.0f} MB"


def _host(base_url: str) -> str:
    """Strip the OpenAI-compatible ``/v1`` back off for native Ollama routes."""
    trimmed = base_url.strip().rstrip("/")
    return trimmed[: -len("/v1")] if trimmed.endswith("/v1") else trimmed


def _ollama(base_url: str, api_key: str | None) -> tuple[list[ModelOption], str]:
    url = f"{_host(base_url)}/api/tags"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = _get_json(url, headers)
    options = []
    for entry in payload.get("models", []):
        name = entry.get("model") or entry.get("name")
        if not name:
            continue
        details = entry.get("details") or {}
        parts = [p for p in (details.get("parameter_size"),) if p]
        if isinstance(entry.get("size"), int):
            parts.append(_human_size(entry["size"]))
        options.append(ModelOption(id=name, label=name, meta=" · ".join(parts)))
    return options, url


def _openai_compatible(
    base_url: str, api_key: str | None
) -> tuple[list[ModelOption], str]:
    url = f"{base_url.strip().rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = _get_json(url, headers)
    options = [
        ModelOption(id=entry["id"], label=entry["id"])
        for entry in payload.get("data", [])
        if entry.get("id")
    ]
    return options, url


def _anthropic(base_url: str, api_key: str | None) -> tuple[list[ModelOption], str]:
    url = "https://api.anthropic.com/v1/models?limit=100"
    payload = _get_json(
        url,
        {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"},
    )
    options = [
        ModelOption(
            id=entry["id"],
            label=entry["id"],
            meta=entry.get("display_name") or "",
        )
        for entry in payload.get("data", [])
        if entry.get("id")
    ]
    return options, url


def _gemini(base_url: str, api_key: str | None) -> tuple[list[ModelOption], str]:
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    payload = _get_json(url, {"x-goog-api-key": api_key or ""})
    options = []
    for entry in payload.get("models", []):
        name = str(entry.get("name", ""))
        if not name.startswith("models/"):
            continue
        # Embedding and legacy models cannot back a chat agent.
        if "generateContent" not in (entry.get("supportedGenerationMethods") or []):
            continue
        model_id = name.removeprefix("models/")
        options.append(
            ModelOption(
                id=model_id,
                label=model_id,
                meta=entry.get("displayName") or "",
            )
        )
    return options, url


_FETCHERS = {
    "ollama": _ollama,
    "ollama-cloud": _ollama,
    "anthropic": _anthropic,
    "gemini": _gemini,
    "openai": _openai_compatible,
    "digitalocean": _openai_compatible,
}


def _get_json(url: str, headers: dict[str, str]) -> dict:
    response = httpx.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _reason(exc: Exception, url: str) -> str:
    """A sentence the GUI can show as-is. Never includes the key."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "The provider rejected the API key."
        return f"The provider answered {code}."
    if isinstance(exc, httpx.TimeoutException):
        return f"No answer from {url} within {TIMEOUT_SECONDS:.0f}s."
    if isinstance(exc, httpx.RequestError):
        return f"Could not reach {url}."
    return f"Unreadable response from {url}: {exc}"


def list_models(
    provider_id: str,
    settings: VikramSettings,
    *,
    refresh: bool = False,
) -> ModelListing:
    """List ``provider_id``'s models, or say why it could not.

    Cached for a minute per provider so switching tabs does not re-ask; pass
    ``refresh`` for the Refresh control, which must always really go and look.
    """
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return ModelListing(
            provider=provider_id,
            ok=False,
            error=f"Unknown provider '{provider_id}'.",
            enumerable=False,
        )

    if provider_id in NOT_ENUMERABLE:
        return ModelListing(
            provider=provider_id,
            ok=False,
            error="Custom endpoints publish no model list.",
            enumerable=False,
        )

    cached = _cache.get(provider_id)
    if cached and not refresh and time.time() - cached.fetched_at < CACHE_TTL_SECONDS:
        return cached

    api_key = (
        getattr(settings, provider.api_key_field, None)
        if provider.api_key_field
        else None
    )
    if provider.needs_api_key and not api_key:
        # Not cached: a key added in Settings should take effect at once.
        return ModelListing(
            provider=provider_id,
            ok=False,
            error=f"{provider.display_name} has no API key yet.",
        )

    base_url = (
        (
            getattr(settings, provider.base_url_field, None)
            if provider.base_url_field
            else None
        )
        or provider.default_base_url
        or ""
    )

    fetcher = _FETCHERS[provider_id]
    url = base_url
    try:
        options, url = fetcher(base_url, api_key)
    except Exception as exc:
        logger.warning("model_listing_failed", provider=provider_id, error=str(exc))
        return ModelListing(provider=provider_id, ok=False, error=_reason(exc, url))

    listing = ModelListing(
        provider=provider_id,
        ok=True,
        models=sorted(options, key=lambda option: option.id),
        source=url,
        fetched_at=time.time(),
    )
    _cache[provider_id] = listing
    return listing


def clear_cache() -> None:
    """Drop every cached listing. Used by tests and by config writes."""
    _cache.clear()
