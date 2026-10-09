"""Discover Ollama's advertised thinking controls without guessing model levels."""

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vikram.evals.history import ThinkingValue


class ThinkingControls(BaseModel):
    model_config = ConfigDict(extra="ignore")

    values: list[ThinkingValue] = Field(min_length=1)
    default: ThinkingValue | None = None

    @model_validator(mode="after")
    def validate_values(self) -> "ThinkingControls":
        if any(isinstance(value, str) and not value.strip() for value in self.values):
            raise ValueError("Thinking levels must not be empty.")
        self.values = list(dict.fromkeys(self.values))
        return self


def discover_thinking(model: str) -> ThinkingControls | None:
    """Read only the advertised values; never persist templates or model prompts."""
    request = Request(
        "http://localhost:11434/api/show",
        data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("Invalid model metadata.")
        metadata = payload.get("thinking")
        return None if metadata is None else ThinkingControls.model_validate(metadata)
    except (URLError, TimeoutError, OSError, ValueError):
        raise ValueError(
            "Could not read thinking controls from local Ollama. "
            "Check that Ollama is running and the model is installed."
        ) from None


def select_levels(
    controls: ThinkingControls | None, requested: list[str]
) -> list[ThinkingValue | None]:
    """Expand all supported levels, or validate an explicit smaller selection."""
    if requested == ["default"]:
        return [None]
    if controls is None:
        raise ValueError(
            "Ollama did not advertise thinking levels. Update Ollama for automatic "
            "benchmark coverage, or use --thinking default for an unswept run."
        )
    if requested == ["all"]:
        return list(controls.values)
    if "all" in requested or "default" in requested:
        raise ValueError("Use all or default alone, or list supported thinking levels.")
    selected: list[ThinkingValue | None] = []
    for item in requested:
        value: ThinkingValue = {"on": True, "off": False}.get(item, item)
        if value not in controls.values:
            raise ValueError(f"Unsupported thinking level: {item}.")
        if value not in selected:
            selected.append(value)
    return selected


def thinking_label(value: ThinkingValue | None) -> str:
    if value is None:
        return "model default (unspecified)"
    if isinstance(value, bool):
        return "on" if value else "off"
    return value


def thinking_request(value: ThinkingValue | None) -> dict[str, str]:
    """Map Ollama controls to its OpenAI-compatible request field."""
    if value is None:
        return {}
    effort = ("high" if value else "none") if isinstance(value, bool) else value
    return {"reasoning_effort": effort}
