"""Classify what changed between two commits, for the eval trigger and record.

Only kinds that can move answer quality trigger a run. Details never include
secrets: MCP servers and hooks are named, never their URL, command or env.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals import gitutil

PROMPT = "prompt"
MODEL = "model"
MODEL_SETTINGS = "model_settings"
MODEL_VERSION = "model_version"
TOOLS = "tools"
MCP_HOOKS_SKILLS = "mcp_hooks_skills"
FRAMEWORK = "framework"
CAPABILITIES = "capabilities"
EVAL_SUITE = "eval_suite"

ALL_KINDS = (
    PROMPT,
    MODEL,
    MODEL_SETTINGS,
    MODEL_VERSION,
    TOOLS,
    MCP_HOOKS_SKILLS,
    FRAMEWORK,
    CAPABILITIES,
    EVAL_SUITE,
)

# Kinds that start a run from the post-commit hook. Every kind is still
# detected and recorded in a run's "what changed"; this only decides whether a
# commit is worth a (slow) run on its own. VIKRAM_EVALS_TRIGGERS overrides it:
# "all", or a comma-separated list of kinds.
TRIGGERS_ENV = "VIKRAM_EVALS_TRIGGERS"
DEFAULT_TRIGGERS = (MODEL, MODEL_SETTINGS, MODEL_VERSION, FRAMEWORK, PROMPT)


def trigger_kinds(value: str | None = None) -> frozenset[str]:
    """The change kinds that make the hook queue a run."""
    import os

    raw = os.environ.get(TRIGGERS_ENV) if value is None else value
    if not raw or not raw.strip():
        return frozenset(DEFAULT_TRIGGERS)
    if raw.strip().lower() == "all":
        return frozenset(ALL_KINDS)
    kinds = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = kinds - set(ALL_KINDS)
    if unknown:
        raise ValueError(
            f"Unknown eval trigger kind(s) in {TRIGGERS_ENV}: "
            f"{', '.join(sorted(unknown))}. Known: {', '.join(ALL_KINDS)}."
        )
    return frozenset(kinds)


ALL = "*"  # every evaluated agent

# (glob, kind, agent). ``{agent}`` takes the second path segment of spec/.
# agent.toml is handled separately because its kind depends on which keys moved.
# First match wins, so spec/shared/ rules precede the spec/<agent>/ ones.
_PATH_RULES: list[tuple[str, str, str]] = [
    ("spec/shared/context/*", PROMPT, ALL),
    ("spec/shared/skills/*", MCP_HOOKS_SKILLS, ALL),
    ("spec/shared/command_policy.toml", TOOLS, ALL),
    ("spec/*/system_prompt.md", PROMPT, "{agent}"),
    ("spec/*/context/*", PROMPT, "{agent}"),
    ("vikram/context.py", PROMPT, ALL),
    ("spec/*/skills/*", MCP_HOOKS_SKILLS, "{agent}"),
    ("vikram/providers.py", MODEL, ALL),
    ("vikram/model_catalog.py", MODEL, ALL),
    ("vikram/tools.py", TOOLS, ALL),
    ("vikram/command_policy.py", TOOLS, ALL),
    ("spec/*/command_policy.toml", TOOLS, "{agent}"),
    ("vikram/mcp.py", MCP_HOOKS_SKILLS, ALL),
    ("vikram/hooks.py", MCP_HOOKS_SKILLS, ALL),
    ("vikram/skills.py", MCP_HOOKS_SKILLS, ALL),
    ("vikram/delegation.py", MCP_HOOKS_SKILLS, ALL),
    ("vikram/agent.py", FRAMEWORK, ALL),
    ("vikram/capabilities.py", CAPABILITIES, ALL),
    ("evals/cases/*.yaml", EVAL_SUITE, "{case_agent}"),
    ("evals/fixtures/*", EVAL_SUITE, ALL),
    ("evals/checks.py", EVAL_SUITE, ALL),
    ("evals/judge.py", EVAL_SUITE, ALL),
]

FRAMEWORK_PACKAGES = ("pydantic-ai-slim", "pydantic-ai-harness")


@dataclass
class ChangeSet:
    base: str
    head: str
    kinds: dict[str, set[str]] = field(default_factory=dict)  # agent -> kinds
    files: dict[str, set[str]] = field(default_factory=dict)  # agent -> files
    details: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, agent: str, kind: str, path: str | None = None) -> None:
        self.kinds.setdefault(agent, set()).add(kind)
        if path:
            self.files.setdefault(agent, set()).add(path)

    def detail(self, agent: str, key: str, value: Any) -> None:
        self.details.setdefault(agent, {})[key] = value

    def agents(self, triggers: frozenset[str] | None = None) -> list[str]:
        """Agents with changes; only changes of ``triggers`` kinds if given."""
        return sorted(
            agent
            for agent, kinds in self.kinds.items()
            if kinds and (triggers is None or kinds & triggers)
        )

    def for_agent(self, agent: str) -> dict[str, Any]:
        return {
            "kinds": sorted(self.kinds.get(agent, set())),
            "files": sorted(self.files.get(agent, set())),
            "details": self.details.get(agent, {}),
        }


def _match(path: str, pattern: str) -> bool:
    # fnmatch's "*" crosses "/", which is what we want for directory trees.
    return fnmatch.fnmatch(path, pattern)


def _resolve_agents(template: str, path: str, agents: list[str]) -> list[str]:
    parts = path.split("/")
    if template == ALL:
        return list(agents)
    if template == "{agent}":
        name = parts[1] if len(parts) > 1 else ""
        return [name] if name in agents else []
    if template == "{case_agent}":
        name = Path(path).stem
        return [name] if name in agents else []
    return [template] if template in agents else []


def _load_toml(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _names(entries: Any) -> list[str]:
    names = []
    for entry in entries or []:
        if isinstance(entry, dict):
            names.append(
                str(entry.get("name") or entry.get("event") or entry.get("entrypoint"))
            )
        else:
            names.append(str(entry))
    return sorted(names)


def _list_delta(before: list[str], after: list[str]) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
    }


def _diff_agent_toml(
    changes: ChangeSet, agent: str, path: str, before: dict, after: dict
) -> None:
    for key in ("model", "model_provider"):
        if before.get(key) != after.get(key):
            changes.add(agent, MODEL, path)
            changes.detail(agent, key, {"from": before.get(key), "to": after.get(key)})
    old_settings = before.get("model_settings") or {}
    new_settings = after.get("model_settings") or {}
    for key in sorted(set(old_settings) | set(new_settings)):
        if old_settings.get(key) != new_settings.get(key):
            changes.add(agent, MODEL_SETTINGS, path)
            changes.detail(
                agent,
                f"model_settings.{key}",
                {"from": old_settings.get(key), "to": new_settings.get(key)},
            )
    if before.get("tools") != after.get("tools"):
        changes.add(agent, TOOLS, path)
        changes.detail(
            agent,
            "tools",
            _list_delta(before.get("tools") or [], after.get("tools") or []),
        )
    for key in ("command_policy", "command_policy_override"):
        if before.get(key) != after.get(key):
            changes.add(agent, TOOLS, path)
            changes.detail(agent, key, "changed")
    for key in ("skills", "shared_skills", "mcp_servers", "hooks"):
        old, new = _names(before.get(key)), _names(after.get(key))
        if old != new or before.get(key) != after.get(key):
            changes.add(agent, MCP_HOOKS_SKILLS, path)
            changes.detail(agent, key, _list_delta(old, new))
    old_caps = before.get("capabilities") or {}
    new_caps = after.get("capabilities") or {}
    for key in sorted(set(old_caps) | set(new_caps)):
        if old_caps.get(key) != new_caps.get(key):
            changes.add(agent, CAPABILITIES, path)
            changes.detail(
                agent,
                f"capabilities.{key}",
                {"from": old_caps.get(key), "to": new_caps.get(key)},
            )
    for key in ("context_files", "shared_context_files", "system_prompt"):
        if before.get(key) != after.get(key):
            changes.add(agent, PROMPT, path)
            changes.detail(agent, key, "changed")


def _lock_versions(text: str | None) -> dict[str, str]:
    data = _load_toml(text)
    return {
        pkg["name"]: pkg.get("version", "")
        for pkg in data.get("package", [])
        if pkg.get("name") in FRAMEWORK_PACKAGES
    }


def detect_changes(repo: Path, base: str, head: str, agents: list[str]) -> ChangeSet:
    """Classify the diff ``base..head`` into change kinds per evaluated agent."""
    changes = ChangeSet(base=base, head=head)
    files = gitutil.changed_files(repo, base, head)
    prompt_files: dict[str, list[str]] = {}

    for path in files:
        if path.startswith("evals/history/"):
            continue
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "spec" and parts[2] == "agent.toml":
            agent = parts[1]
            if agent in agents:
                _diff_agent_toml(
                    changes,
                    agent,
                    path,
                    _load_toml(gitutil.show(repo, base, path)),
                    _load_toml(gitutil.show(repo, head, path)),
                )
            continue
        if path == "uv.lock":
            old = _lock_versions(gitutil.show(repo, base, path))
            new = _lock_versions(gitutil.show(repo, head, path))
            for package in FRAMEWORK_PACKAGES:
                if old.get(package) != new.get(package):
                    for agent in agents:
                        changes.add(agent, FRAMEWORK, path)
                        changes.detail(
                            agent,
                            package,
                            {"from": old.get(package), "to": new.get(package)},
                        )
            continue
        for pattern, kind, template in _PATH_RULES:
            if _match(path, pattern):
                for agent in _resolve_agents(template, path, agents):
                    changes.add(agent, kind, path)
                    if kind == PROMPT:
                        prompt_files.setdefault(agent, []).append(path)
                break

    for agent, paths in prompt_files.items():
        added, removed = gitutil.numstat(repo, base, head, paths)
        changes.detail(agent, "prompt_lines", {"added": added, "removed": removed})
    return changes


def add_model_version_change(
    changes: ChangeSet,
    agent: str,
    model: str | None,
    previous_digest: str | None,
    current_digest: str | None,
) -> None:
    """Record a model rebuild under the same tag (e.g. after ``ollama pull``)."""
    if previous_digest and current_digest and previous_digest != current_digest:
        changes.add(agent, MODEL_VERSION)
        changes.detail(
            agent,
            "model_digest",
            {"model": model, "from": previous_digest, "to": current_digest},
        )
