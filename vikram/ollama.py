"""Shared, metadata-driven Ollama thinking controls."""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

ThinkingValue = StrictBool | StrictStr


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


def thinking_on(
    values: list[ThinkingValue], default: ThinkingValue | None = None
) -> ThinkingValue | None:
    """The owner's default: thinking on unless explicitly off.

    On is `True` when advertised, otherwise the model's own named level. `None`
    means the model advertises no controls, so nothing can be requested.
    """
    if any(value is True for value in values):
        return True
    if isinstance(default, str) and default in values:
        return default
    return next((value for value in values if isinstance(value, str)), None)


def thinking_request(value: ThinkingValue | None) -> dict[str, str]:
    if value is None:
        return {}
    effort = ("high" if value else "none") if isinstance(value, bool) else value
    return {"reasoning_effort": effort}
