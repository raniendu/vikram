"""Validated, content-free run records and pass@k estimates."""

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

DEFAULT_DIRECTORY = Path(".vikram/evals/v1")
DEFAULT_JUDGE = "gemma4:26b-a4b-it-qat"
ThinkingValue = StrictBool | StrictStr


def fingerprint(value: object) -> str:
    """Hash evaluation provenance without storing its source content."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def pass_at_k(n: int, c: int, k: int) -> float:
    """Estimate the probability of at least one success in k independent attempts."""
    if not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError("Require 0 <= passes <= samples and 1 <= k <= samples.")
    if n - c < k:
        return 1.0
    return 1 - math.comb(n - c, k) / math.comb(n, k)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RunConfig(Record):
    model: str = Field(min_length=1)
    judge_model: str | None = DEFAULT_JUDGE
    # None preserves the identity of historical records made before local judging.
    judge_provider: Literal["ollama", "openrouter"] | None = None
    judge_thinking: ThinkingValue | None = None
    samples: int = Field(default=5, ge=1)
    ks: list[int] = Field(default_factory=lambda: [1, 3, 5])
    temperature: float = 0.7
    judge_temperature: float = 0.0
    timeout_seconds: float = Field(default=300.0, gt=0)
    judge_timeout_seconds: float = Field(default=60.0, gt=0)
    thinking: ThinkingValue | None = None
    thinking_levels: list[ThinkingValue] | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if not self.ks or any(k < 1 or k > self.samples for k in self.ks):
            raise ValueError("Every k must be between 1 and samples.")
        self.ks = sorted(set(self.ks) | {1})
        return self


class CaseResult(Record):
    id: str
    label: str
    group: Literal["objective", "judged"]
    samples: int = Field(ge=1)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    generation_errors: int = Field(default=0, ge=0)
    grading_errors: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (
            self.passed + self.failed + self.generation_errors + self.grading_errors
            > self.samples
        ):
            raise ValueError("Case counts exceed the sample budget.")
        return self

    @property
    def unscored(self) -> int:
        return self.samples - self.passed - self.failed

    def scores(self, ks: list[int]) -> dict[str, float] | None:
        if self.unscored:
            return None
        return {str(k): pass_at_k(self.samples, self.passed, k) for k in ks}


class AgentRevision(Record):
    source_hash: str
    dependencies_hash: str
    commit: str | None = None
    dirty: bool | None = None
    harness: dict[str, str] = Field(default_factory=dict)


class RunRecord(Record):
    schema_version: Literal[1] = 1
    run_id: str
    started_at: datetime
    duration_seconds: float = Field(ge=0)
    config: RunConfig
    suite_hash: str
    agent_hash: str
    protocol_hash: str
    interrupted: bool = False
    cases: list[CaseResult] = Field(min_length=1)
    metrics: dict[str, dict[str, float] | None] = Field(default_factory=dict)
    label: str = ""
    experiment: str = ""
    revision: AgentRevision | None = None
    benchmark_version: Literal[2] | None = None

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique.")
        if any(case.samples != self.config.samples for case in self.cases):
            raise ValueError("Case sample budgets must match the run.")
        return self

    @property
    def status(self) -> str:
        if self.interrupted:
            return "interrupted"
        return "incomplete" if any(c.unscored for c in self.cases) else "complete"

    @property
    def comparison_key(self) -> str:
        # The measuring stick is fixed; the agent being measured is allowed to change.
        judged = any(case.group == "judged" for case in self.cases)
        identity = [
            self.benchmark_version,
            self.suite_hash,
            self.protocol_hash,
            self.config.judge_model if judged else None,
            self.config.judge_temperature if judged else None,
            self.config.judge_timeout_seconds if judged else None,
        ]
        if judged and self.config.judge_provider is not None:
            identity.extend([self.config.judge_provider, self.config.judge_thinking])
        return fingerprint(identity)

    @property
    def variant_key(self) -> str:
        return fingerprint([self.configuration_key, self.config.thinking])

    @property
    def configuration_key(self) -> str:
        """Identify an agent setup independently of its thinking level."""
        if self.revision is None:
            # Old history cannot prove that tools/runtime code stayed the same.
            return fingerprint(["legacy", self.run_id])
        return fingerprint(
            [
                self.config.model,
                self.agent_hash,
                self.revision.source_hash,
                self.revision.dependencies_hash,
                self.revision.harness,
                self.config.temperature,
                self.config.timeout_seconds,
            ]
        )

    def scores(
        self, group: str | None = None, ks: list[int] | None = None
    ) -> dict[str, float] | None:
        selected = [c for c in self.cases if group is None or c.group == group]
        if not selected or any(c.unscored for c in selected) or self.interrupted:
            return None
        return {
            str(k): sum(pass_at_k(c.samples, c.passed, k) for c in selected)
            / len(selected)
            for k in (self.config.ks if ks is None else ks)
            if k <= self.config.samples
        }

    def report_data(self, ks: list[int] | None = None) -> dict[str, object]:
        """Only the explicitly modeled metrics enter the HTML document."""
        return {
            **self.model_dump(mode="json"),
            "status": self.status,
            "comparison_key": self.comparison_key,
            "variant_key": self.variant_key,
            "configuration_key": self.configuration_key,
            "scores": {
                "combined": self.scores(ks=ks),
                "objective": self.scores("objective", ks),
                "judged": self.scores("judged", ks),
            },
            "cases": [
                {
                    **case.model_dump(),
                    "unscored": case.unscored,
                    "scores": case.scores(
                        [
                            k
                            for k in (self.config.ks if ks is None else ks)
                            if k <= self.config.samples
                        ]
                    ),
                }
                for case in self.cases
            ],
        }


def atomic_write(path: Path, content: str) -> None:
    """Publish complete files; an interrupted write cannot truncate history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_run(run: RunRecord, directory: Path = DEFAULT_DIRECTORY) -> Path:
    """Append a new metrics record; never serialize Pydantic Evals reports."""
    # Validate mutable counters once more before persisting.
    validated = RunRecord.model_validate(run.model_dump())
    validated.metrics = {
        "combined": validated.scores(),
        "objective": validated.scores("objective"),
        "judged": validated.scores("judged"),
    }
    path = directory / "history" / f"{validated.run_id}.json"
    if path.exists():
        raise FileExistsError("This run is already saved.")
    atomic_write(path, validated.model_dump_json(indent=2) + "\n")
    return path


def load_history(directory: Path = DEFAULT_DIRECTORY) -> list[RunRecord]:
    """Read only the new versioned history; reject malformed records."""
    runs = []
    for path in sorted((directory / "history").glob("*.json")):
        try:
            runs.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError:
            # Validation errors can contain input values; do not echo their bodies.
            raise ValueError(
                f"Invalid evaluation history record: {path.name}"
            ) from None
    return sorted(runs, key=lambda run: (run.started_at, run.run_id))
