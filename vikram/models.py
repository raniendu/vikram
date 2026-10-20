"""Read local model metadata and fail closed for remote/unknown entries."""

import asyncio

import httpx

from vikram.ollama import ThinkingControls

BASE = "http://localhost:11434"


def parse_model(entry: dict, shown: dict) -> dict:
    name = entry.get("name", "")
    capabilities = shown.get("capabilities", [])
    details = shown.get("details", {})
    remote = any(
        p.get(key) for p in (entry, shown) for key in ("remote_host", "remote_model")
    )
    local = (
        not remote
        and isinstance(entry.get("size"), int)
        and entry["size"] > 0
        and bool(entry.get("digest"))
        and details.get("format") in {"gguf", "safetensors"}
        and entry.get("details", {}).get("format") == details.get("format")
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
        reason = "Cloud-backed models are unavailable in this local-only app."
    elif not local:
        reason = "Local model files could not be verified from Ollama metadata."
    elif not available:
        reason = "This model does not advertise chat completion support."
    return {
        "name": name,
        "available": bool(available),
        "reason": reason,
        "vision": bool(available and "vision" in capabilities),
        "tools": bool(available and "tools" in capabilities),
        "can_think": bool(available and "thinking" in capabilities),
        "thinking": list(controls.values) if controls and available else [],
        "size": entry.get("size", 0),
        "context_length": None,
    }


class ModelCatalog:
    async def refresh(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(
                base_url=BASE, timeout=10, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                entries = response.json()["models"]
                if not isinstance(entries, list):
                    raise ValueError()
                gate = asyncio.Semaphore(3)

                async def inspect(entry):
                    async with gate:
                        try:
                            response = await client.post(
                                "/api/show", json={"model": entry["name"]}
                            )
                            response.raise_for_status()
                            return parse_model(entry, response.json())
                        except (httpx.HTTPError, ValueError, KeyError, TypeError):
                            return parse_model(entry, {})

                models = await asyncio.gather(
                    *(
                        inspect(e)
                        for e in entries[:256]
                        if isinstance(e, dict) and isinstance(e.get("name"), str)
                    )
                )
                try:
                    response = await client.get("/api/ps")
                    response.raise_for_status()
                    loaded = {
                        m["name"]: m.get("context_length")
                        for m in response.json().get("models", [])
                    }
                    for model in models:
                        length = loaded.get(model["name"])
                        model["context_length"] = (
                            length if isinstance(length, int) and length > 0 else None
                        )
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass
                return sorted(models, key=lambda m: m["name"].casefold())
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ValueError(
                "Cannot connect to local Ollama. Open Ollama, then choose Refresh."
            ) from None


def prepare_model(models: list[dict], name: str, thinking) -> tuple:
    """Resolve chat settings with automatic tools whenever the model supports them."""
    selected = next((m for m in models if m["name"] == name and m["available"]), None)
    if selected is None:
        raise ValueError(
            "Choose an installed local chat model. Refresh if a model was removed."
        )
    notices = []
    if thinking is not None and not any(
        type(thinking) is type(v) and thinking == v for v in selected["thinking"]
    ):
        thinking = None
        notices.append(
            "The previous thinking setting is unavailable. Using Model default."
        )
    effective = "auto" if selected.get("tools") else "off"
    if effective == "off":
        notices.append(
            "Web search is unavailable for this model. Chat remains available."
        )
    return selected, thinking, effective, " ".join(notices) or None


def thinking_selection(value: str):
    if value == "default":
        return None
    return {"off": False, "on": True}.get(value, value)
