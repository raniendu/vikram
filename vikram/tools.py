from __future__ import annotations

import asyncio
import shlex
import time
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

from parallel import AsyncParallel
from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import ApprovalRequired

from vikram.command_policy import POLICY_FILENAME, CommandPolicy, load_command_policy
from vikram.file_tools import FILE_TOOL_ENTRIES, HarnessFileTool
from vikram.logging import get_logger
from vikram.settings import VikramSettings

logger = get_logger(__name__)

MAX_COMMAND_OUTPUT_CHARS = 12_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60

_ACTIVE_POLICY: CommandPolicy | None = None


def set_command_policy(policy: CommandPolicy) -> None:
    """Install the command policy used by inspect_command/run_command.

    Called once from build_agent with the merged per-agent policy.
    """
    global _ACTIVE_POLICY
    _ACTIVE_POLICY = policy


def _default_policy_path() -> Path:
    return VikramSettings().spec_root / "shared" / POLICY_FILENAME


def _policy() -> CommandPolicy:
    # Lazy default so direct tool calls and unit tests still get the full
    # deny backstop without an explicit set_command_policy() call.
    global _ACTIVE_POLICY
    if _ACTIVE_POLICY is None:
        _ACTIVE_POLICY = load_command_policy(_default_policy_path())
    return _ACTIVE_POLICY


@lru_cache(maxsize=1)
def _parallel_client() -> AsyncParallel:
    settings = VikramSettings()
    if not settings.parallel_api_key:
        raise RuntimeError(
            "PARALLEL_API_KEY is not set. Add it to .env to enable web_search."
        )
    return AsyncParallel(api_key=settings.parallel_api_key)


async def web_search(query: str) -> str:
    """Search the public web for current or factual information.

    Use this when the answer depends on information you do not already know,
    such as recent events, prices, dates, documentation, or anything that may
    have changed.

    Args:
        query: A concise natural-language search query, ideally 3-10 words.
    """
    log = logger.bind(tool="web_search", query_length=len(query))
    try:
        client = _parallel_client()
    except RuntimeError as e:
        log.warning("tool_web_search_unconfigured")
        return str(e)

    start = time.perf_counter()
    try:
        response = await client.search(
            search_queries=[query],
            objective=query,
            mode="basic",
        )
    except Exception:
        log.exception("tool_web_search_failed", duration_ms=_elapsed_ms(start))
        raise
    log.info(
        "tool_web_search_succeeded",
        duration_ms=_elapsed_ms(start),
        result_count=len(response.results or []),
    )
    if not response.results:
        return f"No results for: {query}"

    blocks: list[str] = []
    for r in response.results[:5]:
        title = r.title or r.url
        excerpt = "\n".join(r.excerpts) if r.excerpts else ""
        blocks.append(f"## {title}\n{r.url}\n\n{excerpt}".rstrip())
    return "\n\n---\n\n".join(blocks)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _workspace_root() -> Path:
    return Path.cwd().resolve()


def _refusal(message: str, *, tool: str, reason: str) -> str:
    """Refuse a tool call and record it.

    Refusals are the security-relevant edge of the coding tools (workspace
    escapes, sensitive paths, denied commands), so every one is logged with a
    stable ``reason`` code that is safe to alert on. The human-readable
    ``message`` is what the model sees.
    """
    logger.warning("tool_call_refused", tool=tool, reason=reason)
    return f"Refusing: {message}"


def _truncate_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... output truncated"


async def inspect_command(
    command: str,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_output_chars: int = MAX_COMMAND_OUTPUT_CHARS,
) -> str:
    """Run a read-only inspection command in cwd without an approval prompt.

    Use this for read-only inspection that should run with no human approval,
    such as git status, git branch -a, git remote -v, git log, or git
    rev-parse. Anything that is not classified read-only is refused here — use
    run_command for it instead. The command is parsed with shlex and executed
    without a shell.

    Args:
        command: Command string, e.g. "git status --short".
        timeout_seconds: Seconds to wait before killing the process.
        max_output_chars: Maximum combined output characters to return.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"Invalid command: {exc}"
    if not argv:
        return _refusal(
            "no command was provided.", tool="inspect_command", reason="empty_command"
        )
    decision, reason = _policy().classify(argv, command)
    if decision == "deny":
        return _refusal(
            reason or "this command is not allowed.",
            tool="inspect_command",
            reason="policy_denied",
        )
    if decision != "auto":
        executable = Path(argv[0]).name
        logger.info(
            "tool_command_rejected",
            tool="inspect_command",
            executable=executable,
            decision=decision,
            reason="not_read_only",
        )
        return (
            f"inspect_command only runs read-only commands; "
            f"use run_command for {executable}."
        )
    return await _execute_command(
        command, argv, timeout_seconds, max_output_chars, tool="inspect_command"
    )


async def run_command(
    ctx: RunContext[None],
    command: str,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_output_chars: int = MAX_COMMAND_OUTPUT_CHARS,
) -> str:
    """Run a command in the current working directory, with human approval.

    Use this for validation and state-changing commands such as tests,
    formatters, git add/commit/push, gh pr create, or any other command the
    user asks for. Read-only commands run immediately; everything else pauses
    for explicit human approval (you will see the exact command). A small set
    of catastrophic commands (force push, history rewrite, --no-verify, sudo,
    recursive rm, writes to secret files) is refused even if approved. The
    command is parsed with shlex and executed without a shell.

    Args:
        command: Command string, e.g. "git commit -m \"message\"".
        timeout_seconds: Seconds to wait before killing the process.
        max_output_chars: Maximum combined output characters to return.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"Invalid command: {exc}"
    if not argv:
        return _refusal(
            "no command was provided.", tool="run_command", reason="empty_command"
        )
    decision, reason = _policy().classify(argv, command)
    if decision == "deny":
        return _refusal(
            reason or "this command is not allowed.",
            tool="run_command",
            reason="policy_denied",
        )
    if decision == "approve" and not ctx.tool_call_approved:
        logger.info(
            "tool_command_approval_requested",
            tool="run_command",
            executable=Path(argv[0]).name,
        )
        raise ApprovalRequired()
    return await _execute_command(
        command, argv, timeout_seconds, max_output_chars, tool="run_command"
    )


async def _execute_command(
    command: str,
    argv: list[str],
    timeout_seconds: int,
    max_output_chars: int,
    *,
    tool: str,
) -> str:
    if timeout_seconds < 1:
        return "timeout_seconds must be at least 1."
    if max_output_chars < 1:
        return "max_output_chars must be at least 1."

    # Only the executable and argument count are logged. The full command may
    # carry credentials (tokens passed as flags), so it stays out of the logs.
    log = logger.bind(
        tool=tool,
        executable=Path(argv[0]).name,
        argument_count=len(argv) - 1,
        timeout_seconds=timeout_seconds,
    )
    log.info("tool_command_started")
    start = time.perf_counter()

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=_workspace_root(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("tool_command_not_found", duration_ms=_elapsed_ms(start))
        return f"Command not found: {argv[0]}"
    except OSError as exc:
        log.exception("tool_command_unstartable", duration_ms=_elapsed_ms(start))
        return f"Could not run {command}: {exc}"

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        log.warning("tool_command_timed_out", duration_ms=_elapsed_ms(start))
        return _truncate_output(
            f"$ {command}\nTimed out after {timeout_seconds}s.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}".rstrip(),
            max_output_chars,
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    log.info(
        "tool_command_finished",
        duration_ms=_elapsed_ms(start),
        exit_code=process.returncode,
        stdout_length=len(stdout),
        stderr_length=len(stderr),
    )
    sections = [f"$ {command}", f"exit code: {process.returncode}"]
    if stdout:
        sections.append(f"stdout:\n{stdout}")
    if stderr:
        sections.append(f"stderr:\n{stderr}")
    return _truncate_output("\n".join(sections), max_output_chars)


ToolEntry = Callable[..., Awaitable[str]] | Tool[None] | HarnessFileTool


TOOL_REGISTRY: dict[str, ToolEntry] = {
    "web_search": web_search,
    # Served by the harness FileSystem capability (vikram/file_tools.py).
    "read_file": FILE_TOOL_ENTRIES["read_file"],
    "glob": FILE_TOOL_ENTRIES["glob"],
    "grep": FILE_TOOL_ENTRIES["grep"],
    "inspect_command": inspect_command,
    "write_file": FILE_TOOL_ENTRIES["write_file"],
    "edit_file": FILE_TOOL_ENTRIES["edit_file"],
    # run_command decides approval dynamically for Tier-2 commands. Sequential
    # execution prevents concurrent state-changing shell operations.
    "run_command": Tool(run_command, sequential=True),
}
