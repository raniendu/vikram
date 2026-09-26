"""Workspace file tools backed by the Pydantic AI Harness ``FileSystem``.

Specs keep naming the tools Vikram always had (``read_file``, ``glob``,
``grep``, ``write_file``, ``edit_file``); the harness does the file work. The
layers, outermost first::

    HookToolset                                # PreToolUse/PostToolUse hooks
      approval_required(write_file, edit_file) # Vikram's approval prompt
        _VikramFileGuard                       # names, refusals as text, logs
          _VikramFileSystemToolset             # harness FileSystem, Vikram's
                                               # sensitive/skipped path rules

all served by :class:`VikramFileSystem`, a harness ``FileSystem`` capability.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_ai_harness import FileSystem
from pydantic_ai_harness.filesystem import FileSystemToolset

from vikram.logging import get_logger

logger = get_logger(__name__)

MAX_FILE_LINES = 200
MAX_GLOB_MATCHES = 200
MAX_GREP_MATCHES = 100

# Vikram name -> harness tool name.
HARNESS_NAMES = {
    "read_file": "read_file",
    "glob": "find_files",
    "grep": "grep",
    "write_file": "write_file",
    "edit_file": "edit_file",
}
WRITE_TOOLS = frozenset({"write_file", "edit_file"})

SKIPPED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.production.credentials",
    ".env.production.generated",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}
SENSITIVE_DIR_NAMES = {".ssh", ".terraform", "secrets"}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx", ".tfstate", ".tfstate.backup"}

_DENIED_MARKER = "vikram-sensitive-or-skipped"


@dataclass(frozen=True)
class HarnessFileTool:
    """A ``TOOL_REGISTRY`` entry served by the harness file toolset.

    Carries the metadata the tool catalog, ACP and the GUI read
    (``requires_approval``, ``sequential``, a description) without being a
    callable Vikram registers itself.
    """

    name: str
    description: str
    requires_approval: bool = False
    sequential: bool = False


def is_denied_path(relative: str) -> bool:
    """Vikram's rule for paths no file tool may touch or list.

    Secrets (``.env*`` except ``.env.example``, keys, ``.ssh``, ``secrets/``,
    Terraform state) and tool/VCS directories (``.git``, ``.venv``, …).
    """
    for part in PurePosixPath(relative).parts:
        lower = part.lower()
        if lower in SKIPPED_DIR_NAMES:
            return True
        if lower == ".env.example":
            continue
        if lower.startswith(".env"):
            return True
        if lower in SENSITIVE_NAMES or lower in SENSITIVE_DIR_NAMES:
            return True
        if any(lower.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
            return True
    return False


class _VikramFileSystemToolset(FileSystemToolset):
    """Harness FileSystem with Vikram's exact sensitive/skipped path rules.

    Glob patterns can't say "every ``.env*`` except ``.env.example``", so the
    denied-pattern check asks :func:`is_denied_path` instead. It gates reads,
    writes and what find/grep list. ``pydantic-ai-harness`` is pinned exactly
    and ``tests/test_file_tools.py`` covers this hook.
    """

    def _first_matching_pattern(self, path: str, patterns: list[str]) -> str | None:
        if patterns is self._denied_patterns:
            return _DENIED_MARKER if is_denied_path(path) else None
        return super()._first_matching_pattern(path, patterns)


def _refusal_reason(message: str) -> str | None:
    if "outside the root directory" in message or "symlink loop" in message:
        return "path_escapes_workspace"
    if _DENIED_MARKER in message or "is protected" in message:
        return "sensitive_path"
    return None


@dataclass
class _VikramFileGuard(WrapperToolset[Any]):
    """Keeps the behaviour Vikram's own file tools had.

    - A refused path answers ``Refusing: …`` instead of raising ``ModelRetry``:
      a local model that keeps asking for ``.env`` must not exhaust its retries
      and fail the run.
    - ``write_file`` creates missing parent directories inside the workspace.
    - Writes are sequential, and successes/refusals are logged by path and size
      only, with the same event names as before.
    """

    root: Path = Path(".")
    # Vikram name -> harness name. Done here rather than with ``.renamed()``:
    # that rewrites ``ctx.tool_name``, and the harness's capability events are
    # then rejected because Pydantic AI can no longer find the owning tool.
    renames: dict[str, str] = field(default_factory=dict)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, Any]:
        tools = await super().get_tools(ctx)
        to_vikram = {harness: vikram for vikram, harness in self.renames.items()}
        exposed: dict[str, Any] = {}
        for name, tool in tools.items():
            public = to_vikram.get(name, name)
            tool_def = replace(
                tool.tool_def, name=public, sequential=public in WRITE_TOOLS
            )
            exposed[public] = replace(tool, toolset=self, tool_def=tool_def)
        return exposed

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: Any
    ) -> Any:
        path = str(tool_args.get("path", "."))
        existed = False
        if name == "write_file":
            existed = self._ensure_parent(path)
        harness_name = self.renames.get(name, name)
        inner_tool = replace(tool, tool_def=replace(tool.tool_def, name=harness_name))
        try:
            result = await self.wrapped.call_tool(
                harness_name, tool_args, ctx, inner_tool
            )
        except ModelRetry as exc:
            message = str(exc)
            reason = _refusal_reason(message)
            if reason is None:
                logger.info("tool_file_call_rejected", tool=name)
                return message
            logger.warning("tool_call_refused", tool=name, reason=reason)
            if reason == "path_escapes_workspace":
                return f"Refusing: {path!r} escapes the workspace."
            return f"Refusing: {path!r} is a secret or excluded path."
        if name == "write_file":
            logger.info(
                "tool_write_file_succeeded",
                path=path,
                content_length=len(str(tool_args.get("content", ""))),
                overwrote_existing=existed,
            )
        elif name == "edit_file":
            logger.info("tool_edit_file_succeeded", path=path, replacements=1)
        return result

    def _ensure_parent(self, path: str) -> bool:
        """Create the parent directory of ``path``; return whether it existed.

        Only inside the workspace and never under a denied path: anything else
        is left for the harness to refuse.
        """
        root = self.root.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            return False
        relative = target.relative_to(root).as_posix()
        if is_denied_path(relative):
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.exists()


@dataclass
class VikramFileSystem(FileSystem):
    """The harness ``FileSystem`` capability with Vikram's layers around it.

    It has to be a capability: the harness toolset emits capability events,
    which Pydantic AI only accepts from capability-contributed tools. Vikram's
    ``PreToolUse``/``PostToolUse`` hooks normally wrap the agent's toolsets, so
    they are applied here too, outermost, exactly as for the other tools.
    """

    tool_selection: tuple[str, ...] = ()
    hooks: Any = None  # vikram.hooks.HookSet
    agent_name: str = ""

    def get_toolset(self) -> AbstractToolset[Any]:
        root = Path(self.root_dir).resolve()
        inner = _VikramFileSystemToolset(
            root_dir=root,
            allowed_patterns=[],
            denied_patterns=[_DENIED_MARKER],
            protected_patterns=[],
            max_read_lines=self.max_read_lines,
            max_list_results=self.max_list_results,
            max_search_results=self.max_search_results,
            max_find_results=self.max_find_results,
            content_hashes=self.content_hashes,
            tools=[HARNESS_NAMES[name] for name in self.tool_selection],
        )
        renames = {
            vikram: harness
            for vikram, harness in HARNESS_NAMES.items()
            if vikram in self.tool_selection and vikram != harness
        }
        toolset: AbstractToolset[Any] = _VikramFileGuard(
            wrapped=inner, root=root, renames=renames
        ).approval_required(
            lambda ctx, tool_def, tool_args: tool_def.name in WRITE_TOOLS
        )
        if self.hooks is not None and self.hooks.has_tool_hooks:
            from vikram.hooks import HookToolset

            toolset = HookToolset(
                toolset,
                pre=self.hooks.pre,
                post=self.hooks.post,
                agent_name=self.agent_name,
            )
        return toolset


def ensure_ripgrep_on_path() -> bool:
    """Make the bundled ``rg`` findable; return whether ``rg`` is available.

    The harness ``grep`` runs ``rg`` from ``PATH``. The ``coder`` extra installs
    it next to Vikram's interpreter, which is on ``PATH`` in Docker but not
    for a ``uv tool`` install. The directory is *appended*, so anything the
    user already has (their own ``python``, ``rg``) still wins.
    """
    if shutil.which("rg"):
        return True
    bundled = Path(sys.executable).parent
    if not (bundled / "rg").exists():
        logger.warning("ripgrep_unavailable")
        return False
    os.environ["PATH"] = os.pathsep.join(
        part for part in (os.environ.get("PATH", ""), str(bundled)) if part
    )
    logger.info("ripgrep_path_appended")
    return True


def build_file_capability(
    tool_names: list[str],
    *,
    root: Path | None = None,
    hooks: Any = None,
    agent_name: str = "",
) -> VikramFileSystem | None:
    """The harness-backed file capability for the file tools a spec names."""
    selected = tuple(name for name in tool_names if name in HARNESS_NAMES)
    if not selected:
        return None
    if "grep" in selected:
        ensure_ripgrep_on_path()
    return VikramFileSystem(
        root_dir=(root or Path.cwd()).resolve(),
        protected_patterns=[],
        max_read_lines=MAX_FILE_LINES,
        max_list_results=MAX_GLOB_MATCHES,
        max_search_results=MAX_GREP_MATCHES,
        max_find_results=MAX_GLOB_MATCHES,
        # Single-writer agent: hashes would only add tokens to every read.
        content_hashes=False,
        tools=[HARNESS_NAMES[name] for name in selected],
        tool_selection=selected,
        hooks=hooks,
        agent_name=agent_name,
        id="vikram-files",
    )


FILE_TOOL_ENTRIES: dict[str, HarnessFileTool] = {
    "read_file": HarnessFileTool(
        "read_file",
        "Read a text file from the workspace, with line numbers and a content hash.",
    ),
    "glob": HarnessFileTool(
        "glob", "Find files in the workspace whose paths match a glob pattern."
    ),
    "grep": HarnessFileTool(
        "grep", "Search file contents in the workspace with a regular expression."
    ),
    "write_file": HarnessFileTool(
        "write_file",
        "Create or overwrite a workspace file, after human approval.",
        requires_approval=True,
        sequential=True,
    ),
    "edit_file": HarnessFileTool(
        "edit_file",
        "Replace one unique text fragment in a workspace file, after human approval.",
        requires_approval=True,
        sequential=True,
    ),
}
