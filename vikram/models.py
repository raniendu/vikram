"""Discover local models by default, with explicit OpenRouter discovery."""

import asyncio
import copy
import time
from typing import NamedTuple

import httpx

from vikram import sandbox
from vikram.ollama import ThinkingControls, ThinkingValue, thinking_on
from vikram.providers import (
    CLOUD_DISCLOSURE,
    OPENROUTER_BASE,
    CredentialError,
    CredentialStore,
    ModelRef,
    parse_openrouter_model,
)

BASE = "http://localhost:11434"


def parse_model(entry: dict, shown: dict) -> dict:
    shown = shown if isinstance(shown, dict) else {}
    name = entry.get("name", "")
    capabilities = shown.get("capabilities", [])
    details = shown.get("details")
    entry_details = entry.get("details")
    remote = any(
        p.get(key) for p in (entry, shown) for key in ("remote_host", "remote_model")
    )
    local = (
        not remote
        and type(entry.get("size")) is int
        and entry["size"] > 0
        and bool(entry.get("digest"))
        and isinstance(details, dict)
        and isinstance(entry_details, dict)
        and details.get("format") in {"gguf", "safetensors"}
        and entry_details.get("format") == details.get("format")
    )
    available = (
        local and isinstance(capabilities, list) and "completion" in capabilities
    )
    controls = None
    try:
        if shown.get("thinking") is not None:
            controls = ThinkingControls.model_validate(shown["thinking"])
    except ValueError:
        pass
    reason = None
    if remote:
        reason = (
            "Cloud-backed Ollama models are unavailable. Select OpenRouter explicitly."
        )
    elif not local:
        reason = "Local model files could not be verified from Ollama metadata."
    elif not available:
        reason = "This model does not advertise chat completion support."
    return {
        "name": name,
        "provider": "ollama",
        "model_id": name,
        "available": bool(available),
        "reason": reason,
        "vision": bool(available and "vision" in capabilities),
        "tools": bool(available and "tools" in capabilities),
        "can_think": bool(available and "thinking" in capabilities),
        "thinking": list(controls.values) if controls and available else [],
        "thinking_default": controls.default if controls and available else None,
        "size": entry.get("size", 0),
        "context_length": None,
    }


class ModelCatalog:
    def __init__(
        self,
        *,
        credentials: CredentialStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.credentials = credentials or CredentialStore()
        self.http_client = http_client
        self.errors: dict[str, str] = {}
        self._cloud_cache: list[dict] = []
        self._cloud_cached_at: float | None = None
        self._cloud_lock = asyncio.Lock()

    async def refresh(
        self, *, include_cloud: bool = False, force: bool = False
    ) -> list[dict]:
        """Credentials alone never cause cloud requests. Cache public metadata only."""
        self.errors.clear()
        if self.http_client is not None:
            return await self._refresh(self.http_client, include_cloud, force)
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False
        ) as client:
            return await self._refresh(client, include_cloud, force)

    async def _refresh(self, client, include_cloud: bool, force: bool) -> list[dict]:
        try:
            models = await self._local(client)
        except ValueError as error:
            self.errors["ollama"] = str(error)
            if not include_cloud:
                raise
            models = []
        if include_cloud:
            models.extend(await self._cloud(client, force=force))
        return sorted(models, key=lambda m: m["name"].casefold())

    async def _local(self, client: httpx.AsyncClient) -> list[dict]:
        try:
            response = await client.get(f"{BASE}/api/tags", timeout=10)
            response.raise_for_status()
            entries = response.json()["models"]
            if not isinstance(entries, list):
                raise ValueError()
            gate = asyncio.Semaphore(3)

            async def inspect(entry):
                async with gate:
                    try:
                        response = await client.post(
                            f"{BASE}/api/show",
                            json={"model": entry["name"]},
                            timeout=10,
                        )
                        response.raise_for_status()
                        return parse_model(entry, response.json())
                    except (
                        httpx.HTTPError,
                        ValueError,
                        KeyError,
                        TypeError,
                        AttributeError,
                    ):
                        return parse_model(entry, {})

            models = await asyncio.gather(
                *(
                    inspect(e)
                    for e in entries[:256]
                    if isinstance(e, dict) and isinstance(e.get("name"), str)
                )
            )
            try:
                response = await client.get(f"{BASE}/api/ps", timeout=10)
                response.raise_for_status()
                loaded = {
                    m["name"]: m.get("context_length")
                    for m in response.json().get("models", [])
                }
                for model in models:
                    length = loaded.get(model["name"])
                    model["context_length"] = (
                        length if type(length) is int and length > 0 else None
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                pass
            return models
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError(
                "Cannot connect to local Ollama. Open Ollama, then choose Refresh."
            ) from None

    async def _cloud(self, client: httpx.AsyncClient, *, force: bool) -> list[dict]:
        authenticated = False
        try:
            authenticated = bool(self.credentials.get("openrouter"))
        except CredentialError as error:
            self.errors["openrouter"] = str(error)
        stale = False
        async with self._cloud_lock:
            if (
                force
                or self._cloud_cached_at is None
                or time.monotonic() - self._cloud_cached_at >= 300
            ):
                try:
                    entries = await self._cloud_entries(client)
                    self._cloud_cache = entries
                    self._cloud_cached_at = time.monotonic()
                except (
                    httpx.HTTPError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                ):
                    self.errors["openrouter"] = (
                        "Cannot refresh OpenRouter models. Check your connection and retry."
                    )
                    self._cloud_cached_at = None
                    stale = True
            models = []
            for entry in self._cloud_cache:
                try:
                    model = parse_openrouter_model(entry, authenticated=authenticated)
                except (ValueError, TypeError):
                    continue
                if stale:
                    model.update(
                        available=False, reason=self.errors["openrouter"], stale=True
                    )
                models.append(model)
            return copy.deepcopy(models)

    @staticmethod
    async def _cloud_entries(client: httpx.AsyncClient) -> list[dict]:
        # This is public metadata. Never send the inference credential here.
        url = httpx.URL(f"{OPENROUTER_BASE}/models")
        seen, entries = set(), []
        while url is not None:
            if str(url) in seen or len(seen) >= 16:
                raise ValueError("Invalid model pagination.")
            if (
                url.scheme != "https"
                or url.host != "openrouter.ai"
                or url.port not in (None, 443)
                or url.path != "/api/v1/models"
            ):
                raise ValueError("Invalid model pagination.")
            seen.add(str(url))
            request = client.build_request("GET", url, timeout=10)
            request.headers.pop("authorization", None)
            request.headers.pop("cookie", None)
            response = await client.send(request, auth=None, follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            batch = payload["data"]
            if not isinstance(batch, list) or len(entries) + len(batch) > 8192:
                raise ValueError("Invalid model catalog.")
            entries.extend(e for e in batch if isinstance(e, dict))
            next_page = (payload.get("links") or {}).get("next")
            url = (
                url.join(next_page)
                if isinstance(next_page, str) and next_page
                else None
            )
        return entries


class Prepared(NamedTuple):
    model: dict
    # The saved choice: None is the default (on), False is explicitly off.
    setting: ThinkingValue | None
    # What the reply requests: the setting, or "on" for this model by default.
    thinking: ThinkingValue | None
    web: str
    notice: str | None


def prepare_model(models: list[dict], name: str, setting) -> Prepared:
    """Resolve chat settings: thinking on unless explicitly off, and automatic
    tools whenever the model supports them."""
    requested = ModelRef.parse(name)
    selected = next(
        (
            m
            for m in models
            if ModelRef.parse(m["name"]) == requested and m["available"]
        ),
        None,
    )
    if selected is None:
        if requested.is_cloud:
            raise ValueError(
                "Choose an available OpenRouter chat model. Sign in and refresh the model list."
            )
        raise ValueError(
            "Choose an installed local chat model. Refresh if a model was removed."
        )
    notices = [CLOUD_DISCLOSURE] if requested.is_cloud else []
    if setting is not None and not any(
        type(setting) is type(v) and setting == v for v in selected["thinking"]
    ):
        setting = None
        notices.append(
            "The previous thinking setting is unavailable. "
            + (
                "Using thinking on."
                if selected["thinking"]
                else "This model does not advertise thinking controls."
            )
        )
    thinking = (
        thinking_on(selected["thinking"], selected.get("thinking_default"))
        if setting is None
        else setting
    )
    effective = "auto" if selected.get("tools") else "off"
    if effective == "off":
        notices.append(
            "Web search, the Python sandbox, and questions are unavailable for "
            "this model. Chat remains available."
        )
    elif (reason := sandbox.unavailable_reason()) is not None:
        notices.append(f"{reason} Questions, plan mode, and web search still work.")
    return Prepared(selected, setting, thinking, effective, " ".join(notices) or None)


def thinking_selection(value: str):
    """CLI choice to saved setting: on (the default) resolves per model."""
    if value in ("on", "default"):
        return None
    return {"off": False}.get(value, value)
