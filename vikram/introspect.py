"""Read-only introspection of tools, providers, skills and draft specs.

An editor needs to render what an agent *could* be composed from, and to tell
the user why a draft will not build, without running a model. Everything here
reuses the runtime's own registries and error types rather than restating the
rules, so the editor cannot drift from what ``build_agent`` actually accepts.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic_ai import Tool

from vikram.delegation import DELEGATE_TOOL_NAME
from vikram.logging import get_logger
from vikram.providers import PROVIDER_IDS, PROVIDERS
from vikram.settings import VikramSettings, resolve_agent_model_selection
from vikram.skills import SkillError, load_skill
from vikram.spec import AgentSpec, AgentSpecDraft
from vikram.tools import TOOL_REGISTRY

logger = get_logger(__name__)

Severity = Literal["error", "warning"]
Approval = Literal["always", "policy", "never"]

# Approval for these is decided per call against the command policy, so a
# static catalog cannot answer yes or no -- reporting "never" would contradict
# their own docstrings.
POLICY_APPROVAL_TOOLS = frozenset({"run_command"})


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    requires_approval: bool
    sequential: bool
    approval: Approval = "never"


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    display_name: str
    needs_api_key: bool
    api_key_env: str | None
    prompt_base_url: bool
    base_url_hint: str | None
    default_base_url: str | None
    suggested_model: str | None
    configured_model: str | None
    has_credential: bool
    base_url: str | None


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    origin: Literal["agent", "shared"]
    path: str
    resources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationIssue:
    field: str | None
    severity: Severity
    message: str
    fix: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    issues: list[ValidationIssue]
    system_prompt: str | None = None
    tool_names: list[str] = field(default_factory=list)
    approval_tool_names: list[str] = field(default_factory=list)
    model_config: dict[str, Any] | None = None


def _first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    for block in inspect.cleandoc(doc).split("\n\n"):
        text = block.strip()
        if text:
            return " ".join(text.split())
    return ""


def _entry_callable(entry: Any) -> Any:
    return entry.function if isinstance(entry, Tool) else entry


def tool_catalog() -> list[ToolInfo]:
    """Every tool an agent spec may name, with its approval semantics.

    ``approval`` is three-valued because ``run_command`` sits between the
    other two: its ``Tool`` wrapper sets no flag, but it raises for anything
    the command policy classifies as needing a human.
    """
    infos = []
    for name, entry in TOOL_REGISTRY.items():
        always = bool(getattr(entry, "requires_approval", False))
        infos.append(
            ToolInfo(
                name=name,
                description=_first_paragraph(
                    getattr(_entry_callable(entry), "__doc__", "")
                ),
                requires_approval=always,
                sequential=bool(getattr(entry, "sequential", False)),
                approval=(
                    "always"
                    if always
                    else "policy" if name in POLICY_APPROVAL_TOOLS else "never"
                ),
            )
        )
    # delegate_to_agent is built per-agent and bypasses TOOL_REGISTRY, but a
    # spec may still name it, so the editor has to offer it.
    infos.append(
        ToolInfo(
            name=DELEGATE_TOOL_NAME,
            description=(
                "Delegate a self-contained task to another configured Vikram agent."
            ),
            requires_approval=True,
            sequential=True,
            approval="always",
        )
    )
    return sorted(infos, key=lambda info: info.name)


def provider_catalog(settings: VikramSettings) -> list[ProviderInfo]:
    """Providers with their credential status. Never returns key material."""
    provider_models = getattr(settings, "provider_models", {}) or {}
    infos: list[ProviderInfo] = []
    for provider_id in PROVIDER_IDS:
        provider = PROVIDERS[provider_id]
        credential = (
            getattr(settings, provider.api_key_field, None)
            if provider.api_key_field
            else None
        )
        base_url = (
            getattr(settings, provider.base_url_field, None)
            if provider.base_url_field
            else None
        )
        infos.append(
            ProviderInfo(
                id=provider.id,
                display_name=provider.display_name,
                needs_api_key=provider.needs_api_key,
                api_key_env=provider.api_key_env,
                prompt_base_url=provider.prompt_base_url,
                base_url_hint=provider.base_url_hint,
                default_base_url=provider.default_base_url,
                suggested_model=provider.suggested_model,
                configured_model=provider_models.get(provider_id),
                has_credential=bool(credential),
                base_url=base_url or provider.default_base_url,
            )
        )
    return infos


def _skills_in(root: Path, origin: Literal["agent", "shared"]) -> list[SkillInfo]:
    if not root.is_dir():
        return []
    infos: list[SkillInfo] = []
    for skill_dir in sorted(root.iterdir()):
        if not (skill_dir / "SKILL.md").is_file():
            continue
        try:
            skill = load_skill(skill_dir)
        except SkillError as exc:
            logger.warning("skill_unreadable", path=str(skill_dir), error=str(exc))
            continue
        infos.append(
            SkillInfo(
                name=skill.name,
                description=skill.description,
                origin=origin,
                path=str(skill_dir),
                resources=list(skill.resources),
            )
        )
    return infos


def skill_catalog(
    settings: VikramSettings, *, agent_id: str | None = None
) -> dict[str, list[SkillInfo]]:
    """Skills available to an agent: its own, plus the shared ones."""
    from vikram.specstore import find_agent_root, shared_root

    shared = _skills_in(shared_root(settings) / "skills", "shared")
    agent: list[SkillInfo] = []
    if agent_id:
        root = find_agent_root(agent_id, settings)
        if root is not None:
            agent = _skills_in(root.path / agent_id / "skills", "agent")
    return {"agent": agent, "shared": shared}


def validate_draft(
    draft: AgentSpecDraft,
    *,
    settings: VikramSettings,
    agent_dir: Path,
    shared_dir: Path,
) -> ValidationReport:
    """Dry-run a draft and report why it would not build.

    Builds with ``apply_command_policy=False``: this runs while other agents
    may be mid-run, and ``set_command_policy`` writes a process-global.
    """
    issues: list[ValidationIssue] = []

    known = set(TOOL_REGISTRY) | {DELEGATE_TOOL_NAME}
    for name in draft.tools:
        if name not in known:
            issues.append(
                ValidationIssue(
                    field="tools",
                    severity="error",
                    message=f"Unknown tool '{name}'.",
                    fix=f"Choose from: {', '.join(sorted(known))}.",
                )
            )

    provider, model = resolve_agent_model_selection(
        settings,
        agent_id=agent_dir.name,
        spec_provider=draft.model_provider,
        spec_model=draft.model,
    )
    if provider is None:
        issues.append(
            ValidationIssue(
                field="model_provider",
                severity="error",
                message="No model provider resolves for this agent.",
                fix="Pin one on the agent, or set a default with `vikram configure`.",
            )
        )
    elif model is None:
        issues.append(
            ValidationIssue(
                field="model",
                severity="error",
                message=f"No model resolves for provider '{provider}'.",
                fix="Pin a model on the agent or configure one for the provider.",
            )
        )
    elif PROVIDERS[provider].needs_api_key:
        key_field = PROVIDERS[provider].api_key_field
        if key_field and not getattr(settings, key_field, None):
            issues.append(
                ValidationIssue(
                    field="model_provider",
                    severity="warning",
                    message=f"Provider '{provider}' has no credential configured.",
                    fix=f"Set {PROVIDERS[provider].api_key_env} or run `vikram configure`.",
                )
            )

    spec = AgentSpec(
        **draft.model_dump(),
        agent_dir=agent_dir,
        shared_dir=shared_dir,
    )

    if issues and any(issue.severity == "error" for issue in issues):
        return ValidationReport(ok=False, issues=issues)

    try:
        from vikram.agent import build_agent

        agent = build_agent(
            spec=spec,
            settings=settings,
            surface="gui",
            apply_command_policy=False,
        )
    except Exception as exc:
        issues.append(
            ValidationIssue(
                field=_field_for(exc),
                severity="error",
                message=str(exc),
                fix=None,
            )
        )
        return ValidationReport(ok=False, issues=issues)

    return ValidationReport(
        ok=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        system_prompt=agent.system_prompt,
        tool_names=list(agent.tool_names),
        approval_tool_names=list(agent.approval_tool_names),
        model_config=dict(agent.model_config),
    )


def _field_for(exc: Exception) -> str | None:
    """Best-effort mapping from a build error to the field that caused it."""
    name = type(exc).__name__
    return {
        "AgentToolError": "tools",
        "MCPConfigError": "mcp_servers",
        "SkillError": "skills",
        "CommandPolicyError": "command_policy",
        "HookError": "hooks",
    }.get(name)


__all__ = [
    "ProviderInfo",
    "SkillInfo",
    "ToolInfo",
    "ValidationIssue",
    "ValidationReport",
    "provider_catalog",
    "skill_catalog",
    "tool_catalog",
    "validate_draft",
]
