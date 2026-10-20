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


def thinking_request(value: ThinkingValue | None) -> dict[str, str]:
    if value is None:
        return {}
    effort = ("high" if value else "none") if isinstance(value, bool) else value
    return {"reasoning_effort": effort}
