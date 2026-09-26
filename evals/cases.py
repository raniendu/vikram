"""Eval case definitions loaded from ``evals/cases/<agent>.yaml``."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from evals import CASES_DIR, FIXTURES_DIR, SUITE_DIR

# Files whose content changes what a score means. Editing any of them starts a
# new suite hash, and runs with different hashes are never compared.
_SCORING_FILES = ("checks.py", "judge.py")

Surface = Literal["cli", "acp", "gui", "http", "threaded", "telegram"]


class CheckSpec(BaseModel):
    """One hard check. ``type`` selects the function in ``evals.checks``."""

    type: str
    value: str | None = None
    values: list[str] = Field(default_factory=list)
    pattern: str | None = None
    tool: str | None = None
    any_of: list[str] = Field(default_factory=list)
    args_contains: str | None = None
    path: str | None = None
    paths: list[str] = Field(default_factory=list)
    code: str | None = None


class JudgeSpec(BaseModel):
    rubric: str
    threshold: float = 0.7


class GenerateSpec(BaseModel):
    """Write a synthetic file: ``lines`` copies of ``line``, with ``inject``
    replacing specific 1-based line numbers."""

    path: str
    lines: int
    line: str
    inject: dict[int, str] = Field(default_factory=dict)


class SetupSpec(BaseModel):
    git_init: bool = False
    # Files written after the optional initial commit, so they show up as
    # uncommitted changes when git_init is set.
    write: dict[str, str] = Field(default_factory=dict)
    generate: list[GenerateSpec] = Field(default_factory=list)


class CaseSpec(BaseModel):
    id: str
    prompt: str
    surface: Surface = "cli"
    workspace: str | None = None
    setup: SetupSpec = Field(default_factory=SetupSpec)
    checks: list[CheckSpec] = Field(default_factory=list)
    judge: JudgeSpec | None = None
    tags: list[str] = Field(default_factory=list)
    timeout_seconds: float = 600.0


class Suite(BaseModel):
    agent: str
    cases: list[CaseSpec]


def suite_path(agent: str, suite_dir: Path = SUITE_DIR) -> Path:
    return suite_dir / CASES_DIR.name / f"{agent}.yaml"


def list_agents(suite_dir: Path = SUITE_DIR) -> list[str]:
    return sorted(path.stem for path in (suite_dir / CASES_DIR.name).glob("*.yaml"))


def load_suite(agent: str, suite_dir: Path = SUITE_DIR) -> Suite:
    path = suite_path(agent, suite_dir)
    if not path.is_file():
        raise FileNotFoundError(f"No eval cases for agent {agent!r} ({path}).")
    data: Any = yaml.safe_load(path.read_text())
    suite = Suite.model_validate(data)
    if suite.agent != agent:
        raise ValueError(f"{path} declares agent {suite.agent!r}, expected {agent!r}.")
    ids = [case.id for case in suite.cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path} has duplicate case ids.")
    return suite


def suite_hash(agent: str, suite_dir: Path = SUITE_DIR) -> str:
    """Hash of everything that defines this agent's cases and how they score."""
    digest = hashlib.sha256()
    fixtures = suite_dir / FIXTURES_DIR.name
    files = [suite_path(agent, suite_dir), *(suite_dir / n for n in _SCORING_FILES)]
    files += sorted(p for p in fixtures.rglob("*") if p.is_file())
    for path in files:
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(suite_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]
