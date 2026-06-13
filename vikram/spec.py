from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from vikram.command_policy import POLICY_FILENAME, CommandPolicy, load_command_policy
from vikram.hooks import HookSpec
from vikram.mcp import MCPServerSpec

SHARED_DIR_NAME = "shared"


class AgentSurfaceError(RuntimeError):
    """Raised when a spec is loaded on a surface it explicitly disallows."""


class AgentSpec(BaseModel):
    name: str
    description: str
    system_prompt: Path
    cli_only: bool = False
    context_files: list[Path] = []
    skills: list[Path] = []
    shared_context_files: list[Path] = []
    shared_skills: list[Path] = []
    tools: list[str] = []
    mcp_servers: list[MCPServerSpec] = []
    hooks: list[HookSpec] = []
    model_settings: dict[str, Any] = {}
    command_policy: Path = Path(POLICY_FILENAME)
    command_policy_override: dict[str, Any] = {}

    agent_dir: Path
    shared_dir: Path

    @property
    def instructions(self) -> str:
        parts = [(self.agent_dir / self.system_prompt).read_text().strip()]
        for ctx in self.shared_context_files:
            parts.append((self.shared_dir / ctx).read_text().strip())
        for ctx in self.context_files:
            parts.append((self.agent_dir / ctx).read_text().strip())
        return "\n\n".join(parts)

    def load_command_policy(self) -> CommandPolicy:
        """Load this agent's command policy (shared file + optional override).

        The policy path is resolved relative to the shared spec directory.
        Raises CommandPolicyError if the file is missing or invalid.
        """
        return load_command_policy(
            self.shared_dir / self.command_policy,
            self.command_policy_override or None,
        )


def load_spec(name: str, spec_root: Path) -> AgentSpec:
    spec_path = spec_root / name / "agent.toml"
    data = tomllib.loads(spec_path.read_text())
    return AgentSpec(
        **data,
        agent_dir=spec_path.parent,
        shared_dir=spec_root / SHARED_DIR_NAME,
    )


def ensure_surface_allowed(spec: AgentSpec, surface: str) -> None:
    if spec.cli_only and surface != "cli":
        raise AgentSurfaceError(
            f"Agent {spec.name} is CLI-only and cannot run on {surface}."
        )
