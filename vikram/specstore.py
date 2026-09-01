"""Agent discovery and CRUD across a writable user root and the shipped specs.

Shipped specs live inside the installed wheel, or inside the git checkout that
``vikram update`` fast-forwards. Neither is a safe place to write, so
user-created agents get their own root under ``~/.config/vikram/agents/`` --
deliberately *not* under ``~/.local/share/vikram``, which ``install.sh`` uses
for the source checkout.

The user root shadows the shipped one, so editing a built-in agent copies it
across first and leaves the original intact.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vikram.config import config_dir
from vikram.logging import get_logger
from vikram.settings import VikramSettings, resolve_agent_model_selection
from vikram.spec import (
    SHARED_DIR_NAME,
    AgentSpec,
    AgentSpecDraft,
    load_spec,
)
from vikram.spec_io import (
    SpecWriteError,
    read_agent_toml,
    render_agent_toml,
    write_agent_toml,
)

logger = get_logger(__name__)

SPEC_FILENAME = "agent.toml"
DEFAULT_PROMPT_FILENAME = "system_prompt.md"

# agent_id reaches the filesystem, so it is validated rather than sanitised:
# a rejected name is easier to reason about than a silently rewritten one.
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_AGENT_IDS = frozenset({SHARED_DIR_NAME})

RootKind = Literal["user", "builtin"]


class AgentStoreError(RuntimeError):
    """Raised when an agent cannot be created, updated or removed."""


class AgentNotFoundError(AgentStoreError, FileNotFoundError):
    """Raised when no agent with the requested id exists in any root.

    Also a ``FileNotFoundError`` so the surfaces that already map a missing
    spec to 404 keep doing so -- see ``api.py`` and ``gateway.py``.
    """


class AgentReadOnlyError(AgentStoreError):
    """Raised when a write targets a shipped (built-in) agent."""


@dataclass(frozen=True)
class AgentRoot:
    path: Path
    kind: RootKind
    writable: bool


@dataclass(frozen=True)
class AgentSummary:
    """Enough to render an agent card without building the agent."""

    id: str
    name: str
    description: str
    root: RootKind
    writable: bool
    cli_only: bool
    tools: list[str]
    spec_provider: str | None
    spec_model: str | None
    resolved_provider: str | None
    resolved_model: str | None
    mcp_server_count: int
    hook_count: int
    shadows: RootKind | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentDetail:
    summary: AgentSummary
    draft: AgentSpecDraft | None
    system_prompt: str
    source_toml: str
    path: Path


def ensure_safe_agent_id(agent_id: str) -> None:
    """Reject ids that would escape the agents root.

    Applied on every read, because ``--agent`` and HTTP path parameters reach
    the filesystem. Deliberately weaker than :func:`validate_agent_id`: specs
    that predate the naming rule must keep loading.
    """
    if not agent_id or agent_id in {".", ".."}:
        raise AgentStoreError(f"Invalid agent id {agent_id!r}.")
    if agent_id in RESERVED_AGENT_IDS:
        raise AgentStoreError(f"'{agent_id}' is reserved and cannot name an agent.")
    if os.sep in agent_id or (os.altsep and os.altsep in agent_id):
        raise AgentStoreError(
            f"Invalid agent id {agent_id!r}: must not contain a path."
        )
    if agent_id.startswith(".") or Path(agent_id).is_absolute():
        raise AgentStoreError(f"Invalid agent id {agent_id!r}.")


def validate_agent_id(agent_id: str) -> None:
    """Enforce the naming convention for *newly created* agents.

    Stricter than :func:`ensure_safe_agent_id` so that ids the GUI mints are
    predictable on a case-insensitive filesystem.
    """
    ensure_safe_agent_id(agent_id)
    if not _AGENT_ID.fullmatch(agent_id):
        raise AgentStoreError(
            f"Invalid agent id '{agent_id}'. Use lowercase letters, digits, "
            "'-' and '_', starting with a letter or digit (max 64 characters)."
        )


def user_agents_root() -> Path:
    """Writable root for user-created agents."""
    return config_dir() / "agents"


def shared_root(settings: VikramSettings) -> Path:
    """Shared spec directory. Always the shipped one, whoever owns the agent."""
    return settings.spec_root / SHARED_DIR_NAME


def resolve_roots(settings: VikramSettings) -> list[AgentRoot]:
    """Roots in precedence order: user agents shadow shipped ones."""
    return [
        AgentRoot(path=user_agents_root(), kind="user", writable=True),
        AgentRoot(path=settings.spec_root, kind="builtin", writable=False),
    ]


def _spec_path(root: AgentRoot, agent_id: str) -> Path:
    return root.path / agent_id / SPEC_FILENAME


def find_agent_root(agent_id: str, settings: VikramSettings) -> AgentRoot | None:
    """First root containing ``agent_id``, or ``None``."""
    for root in resolve_roots(settings):
        if _spec_path(root, agent_id).is_file():
            return root
    return None


def load_agent(agent_id: str, settings: VikramSettings) -> AgentSpec:
    """Load an agent from whichever root owns it, with ``shared_dir`` pinned."""
    ensure_safe_agent_id(agent_id)
    root = find_agent_root(agent_id, settings)
    if root is None:
        raise AgentNotFoundError(f"No agent named '{agent_id}'.")
    return load_spec(agent_id, root.path, shared_root=shared_root(settings))


def _summarise(
    agent_id: str,
    root: AgentRoot,
    settings: VikramSettings,
    *,
    shadows: RootKind | None,
) -> AgentSummary:
    """Build a summary, degrading to an error row rather than raising.

    Tolerance here is load-bearing, not politeness: ``discover_subagents``
    iterates every spec while assembling an orchestrator's prompt, so one
    malformed file would otherwise break *every* agent's build on every
    surface. Once users author specs in an editor, that stops being theoretical.
    """
    try:
        spec = load_spec(agent_id, root.path, shared_root=shared_root(settings))
    except Exception as exc:
        logger.warning("agent_spec_unreadable", agent=agent_id, error=str(exc))
        return AgentSummary(
            id=agent_id,
            name=agent_id,
            description="",
            root=root.kind,
            writable=root.writable,
            cli_only=False,
            tools=[],
            spec_provider=None,
            spec_model=None,
            resolved_provider=None,
            resolved_model=None,
            mcp_server_count=0,
            hook_count=0,
            shadows=shadows,
            error=str(exc),
        )

    provider, model = resolve_agent_model_selection(
        settings,
        agent_id=agent_id,
        spec_provider=spec.model_provider,
        spec_model=spec.model,
    )
    return AgentSummary(
        id=agent_id,
        name=spec.name,
        description=spec.description,
        root=root.kind,
        writable=root.writable,
        cli_only=spec.cli_only,
        tools=list(spec.tools),
        spec_provider=spec.model_provider,
        spec_model=spec.model,
        resolved_provider=provider,
        resolved_model=model,
        mcp_server_count=len(spec.mcp_servers),
        hook_count=len(spec.hooks),
        shadows=shadows,
    )


def list_agents(settings: VikramSettings) -> list[AgentSummary]:
    """Every agent across all roots, sorted by id. Never raises on a bad spec."""
    seen: dict[str, AgentRoot] = {}
    shadowed: dict[str, RootKind] = {}
    for root in resolve_roots(settings):
        if not root.path.is_dir():
            continue
        for spec_file in sorted(root.path.glob(f"*/{SPEC_FILENAME}")):
            agent_id = spec_file.parent.name
            if agent_id in RESERVED_AGENT_IDS:
                continue
            if agent_id in seen:
                shadowed[agent_id] = root.kind
                continue
            seen[agent_id] = root
    return [
        _summarise(agent_id, root, settings, shadows=shadowed.get(agent_id))
        for agent_id, root in sorted(seen.items())
    ]


def get_agent(agent_id: str, settings: VikramSettings) -> AgentDetail:
    ensure_safe_agent_id(agent_id)
    root = find_agent_root(agent_id, settings)
    if root is None:
        raise AgentNotFoundError(f"No agent named '{agent_id}'.")

    path = _spec_path(root, agent_id)
    _, source_toml = read_agent_toml(path)
    summary = _summarise(agent_id, root, settings, shadows=None)

    draft: AgentSpecDraft | None = None
    system_prompt = ""
    if summary.error is None:
        spec = load_spec(agent_id, root.path, shared_root=shared_root(settings))
        draft = AgentSpecDraft(**spec.model_dump(exclude={"agent_dir", "shared_dir"}))
        prompt_path = spec.agent_dir / spec.system_prompt
        if prompt_path.is_file():
            system_prompt = prompt_path.read_text(encoding="utf-8")

    return AgentDetail(
        summary=summary,
        draft=draft,
        system_prompt=system_prompt,
        source_toml=source_toml,
        path=path,
    )


def _write(
    agent_id: str,
    draft: AgentSpecDraft,
    *,
    settings: VikramSettings,
    system_prompt: str | None,
    existing_toml: str | None,
) -> AgentDetail:
    target_dir = user_agents_root() / agent_id
    spec_path = target_dir / SPEC_FILENAME
    write_agent_toml(spec_path, render_agent_toml(draft, existing=existing_toml))

    if system_prompt is not None:
        prompt_path = target_dir / draft.system_prompt
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(system_prompt, encoding="utf-8")

    logger.info("agent_spec_written", agent=agent_id, path=str(spec_path))
    return get_agent(agent_id, settings)


def create_agent(
    agent_id: str,
    draft: AgentSpecDraft,
    *,
    settings: VikramSettings,
    system_prompt: str = "",
) -> AgentDetail:
    validate_agent_id(agent_id)
    if find_agent_root(agent_id, settings) is not None:
        raise AgentStoreError(f"An agent named '{agent_id}' already exists.")
    return _write(
        agent_id,
        draft,
        settings=settings,
        system_prompt=system_prompt,
        existing_toml=None,
    )


def update_agent(
    agent_id: str,
    draft: AgentSpecDraft,
    *,
    settings: VikramSettings,
    system_prompt: str | None = None,
) -> AgentDetail:
    """Write a spec to the user root, copying a built-in across if needed.

    Editing a shipped agent never mutates the shipped file: the first write
    lands a user copy that shadows it, which stays reversible by deleting it.
    """
    ensure_safe_agent_id(agent_id)
    root = find_agent_root(agent_id, settings)
    if root is None:
        raise AgentNotFoundError(f"No agent named '{agent_id}'.")

    if root.kind == "builtin":
        _copy_tree(root.path / agent_id, user_agents_root() / agent_id)
        logger.info("agent_copied_on_write", agent=agent_id)

    user_spec = user_agents_root() / agent_id / SPEC_FILENAME
    existing = user_spec.read_text(encoding="utf-8") if user_spec.is_file() else None
    return _write(
        agent_id,
        draft,
        settings=settings,
        system_prompt=system_prompt,
        existing_toml=existing,
    )


def _copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


def duplicate_agent(
    source_id: str,
    new_id: str,
    *,
    settings: VikramSettings,
    name: str | None = None,
) -> AgentDetail:
    ensure_safe_agent_id(source_id)
    validate_agent_id(new_id)
    root = find_agent_root(source_id, settings)
    if root is None:
        raise AgentNotFoundError(f"No agent named '{source_id}'.")
    if find_agent_root(new_id, settings) is not None:
        raise AgentStoreError(f"An agent named '{new_id}' already exists.")

    _copy_tree(root.path / source_id, user_agents_root() / new_id)
    if name is None:
        return get_agent(new_id, settings)

    detail = get_agent(new_id, settings)
    if detail.draft is None:
        raise AgentStoreError(f"Copied spec for '{new_id}' is unreadable.")
    renamed = detail.draft.model_copy(update={"name": name})
    return _write(
        new_id,
        renamed,
        settings=settings,
        system_prompt=None,
        existing_toml=detail.source_toml,
    )


def delete_agent(agent_id: str, settings: VikramSettings) -> None:
    """Remove a user agent. Shipped agents are never deleted."""
    ensure_safe_agent_id(agent_id)
    root = find_agent_root(agent_id, settings)
    if root is None:
        raise AgentNotFoundError(f"No agent named '{agent_id}'.")
    if root.kind == "builtin":
        raise AgentReadOnlyError(
            f"'{agent_id}' is a built-in agent and cannot be deleted."
        )
    shutil.rmtree(user_agents_root() / agent_id)
    logger.info("agent_spec_deleted", agent=agent_id)


__all__ = [
    "AgentDetail",
    "AgentNotFoundError",
    "AgentReadOnlyError",
    "AgentRoot",
    "AgentStoreError",
    "AgentSummary",
    "ensure_safe_agent_id",
    "SpecWriteError",
    "create_agent",
    "delete_agent",
    "duplicate_agent",
    "find_agent_root",
    "get_agent",
    "list_agents",
    "load_agent",
    "resolve_roots",
    "shared_root",
    "update_agent",
    "user_agents_root",
    "validate_agent_id",
]
