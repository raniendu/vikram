"""Trusted host coding tools and their single permission/budget dispatcher.

These tools run with the person's normal host privileges. They are separate
from the isolated Python VM. Adapters, MCP callers and composition brokers
must all call ``dispatch``; only a user-input adapter may set user_initiated.
"""

import asyncio
import difflib
import fnmatch
import hashlib
import json
import os
import shlex
import signal
import stat
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vikram.interaction import ActionRequest, Approver
from vikram.settings import Settings, canonical_workspace

MAX_FILE = 1024 * 1024
MAX_OUTPUT = 16_000
MAX_ENTRIES = 10_000
APPROVAL_TIMEOUT = 15 * 60
Mode = Literal["normal", "plan"]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadArgs(Arguments):
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=200, ge=1, le=2000)


class WriteArgs(Arguments):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=MAX_FILE)
    expected_content: str | None = Field(default=None, max_length=MAX_FILE)


class EditArgs(Arguments):
    path: str = Field(min_length=1, max_length=4096)
    old_text: str = Field(min_length=1, max_length=MAX_FILE)
    new_text: str = Field(max_length=MAX_FILE)


class ListArgs(Arguments):
    path: str = Field(default=".", min_length=1, max_length=4096)
    max_results: int = Field(default=200, ge=1, le=1000)


class FindArgs(ListArgs):
    pattern: str = Field(default="*", min_length=1, max_length=1000)


class GrepArgs(ListArgs):
    pattern: str = Field(min_length=1, max_length=1000)
    glob: str = Field(default="*", min_length=1, max_length=1000)
    ignore_case: bool = False


class BashArgs(Arguments):
    command: str = Field(min_length=1, max_length=20_000)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    timeout: float = Field(default=120.0, gt=0, le=300)


class CodingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    tool: str
    output: str = ""
    diff: str = ""
    paths: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    truncated: bool = False
    error: str | None = None
    activity: str = ""


@dataclass(frozen=True)
class ToolSpec:
    arguments: type[Arguments]
    description: str
    mutation: bool = False

    @property
    def schema(self) -> dict[str, Any]:
        return self.arguments.model_json_schema()


TOOL_SPECS = {
    "read": ToolSpec(ReadArgs, "Read UTF-8 project text with numbered lines."),
    "write": ToolSpec(
        WriteArgs,
        "Create a text file, or replace it only when expected_content matches exactly.",
        True,
    ),
    "edit": ToolSpec(
        EditArgs,
        "Replace exactly one literal old_text occurrence and return its unified diff.",
        True,
    ),
    "grep": ToolSpec(
        GrepArgs, "Find literal text in project files; never follow symlinks."
    ),
    "find": ToolSpec(
        FindArgs, "Find project paths by glob without following symlinks."
    ),
    "ls": ToolSpec(ListArgs, "List a directory with bounded results."),
    "bash": ToolSpec(
        BashArgs,
        "Run a host shell command; unknown or consequential effects need approval.",
        True,
    ),
}


class CodingDenied(ValueError):
    pass


def _sensitive(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts & {".ssh", ".aws", ".gnupg", ".azure", "gcloud"}) or any(
        part == ".env"
        or part.startswith(".env.")
        or part
        in {
            "credentials",
            "credentials.json",
            "secrets.json",
            "trust.json",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            ".docker",
        }
        or part.endswith((".pem", ".key", ".p12", ".pfx", ".keychain-db"))
        for part in parts
    )


def child_environment() -> dict[str, str]:
    """Pass terminal/build basics, never ambient provider/agent credentials."""
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "VIRTUAL_ENV",
        "SDKROOT",
        "DEVELOPER_DIR",
        "MACOSX_DEPLOYMENT_TARGET",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1"})
    return environment


def _shell_reason(command: str, cwd: Path, workspace: Path) -> str | None:
    # Deliberately narrow: shell parsing is not a security boundary. Anything
    # beyond known inspection/build/test forms is presented for approval.
    if any(char in command for char in "\n\r;|&><`$(){}*?["):
        return "This command uses shell composition or redirection."
    try:
        args = shlex.split(command)
    except ValueError:
        return "This command could not be classified."
    if not args:
        return "This command is empty."
    for token in args[1:]:
        if token.startswith("-"):
            if "=" not in token:
                continue
            token = token.partition("=")[2]
        if token.startswith("~") or token.startswith("/") or ".." in Path(token).parts:
            resolved = Path(token).expanduser()
            resolved = (
                (cwd / resolved).resolve()
                if not resolved.is_absolute()
                else resolved.resolve()
            )
            if not resolved.is_relative_to(workspace):
                return "This command refers to a path outside the workspace."
        candidate = (cwd / token).resolve()
        if _sensitive(candidate) or _sensitive(cwd / token):
            return "This command may access credentials."
        if (cwd / token).exists() and not candidate.is_relative_to(workspace):
            return "This command follows a path outside the workspace."
    executable = Path(args[0]).name
    if args[0] != executable:
        return "This command selects an explicit executable path."
    if executable in {"pwd", "ls", "cat", "head", "tail", "wc"}:
        return None
    if executable in {"rg", "grep"}:
        return "Shell searches may traverse credentials or symlinks; use the grep tool for workspace search."
    if executable == "git" and len(args) >= 2:
        if args[1] in {
            "status",
            "diff",
            "log",
            "show",
            "rev-parse",
            "ls-files",
        } and not any(
            arg.startswith(("--output", "--ext-diff", "--textconv", "--exec-path"))
            for arg in args[2:]
        ):
            if args[1] in {"diff", "log", "show"}:
                return "Git output may include credential files or configured external helpers."
            if (workspace / ".git").is_file() or (workspace / ".git").is_symlink():
                return "This checkout uses Git metadata outside the workspace."
            return None
        return "Git changes, remote operations, and unclassified Git effects need approval."
    if executable == "uv" and args[1:3] == ["run", "--no-sync"]:
        args = args[3:]
        executable = args[0] if args else ""
    if executable in {"pytest", "py.test"}:
        return None
    if executable in {"python", "python3", "python3.13"} and len(args) >= 3:
        if args[1:3] in (["-m", "pytest"], ["-m", "unittest"], ["-m", "compileall"]):
            return None
    if executable == "swift" and len(args) >= 2 and args[1] in {"build", "test"}:
        return None
    if (
        executable == "cargo"
        and len(args) >= 2
        and args[1] in {"build", "test", "check"}
    ):
        return None
    if executable in {"make", "ninja"} and all(
        token in {"build", "test", "check", "all"} for token in args[1:]
    ):
        return None
    return "This command's effects are unclassified and require approval."


@contextmanager
def _parent_descriptor(path: Path, *, create: bool = False):
    """Walk canonical ancestors without symlinks, keeping the parent pinned."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


def _read_text(path: Path) -> str:
    with _parent_descriptor(path) as parent:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Only regular text files can be read.")
            value = handle.read(MAX_FILE + 1)
    if len(value) > MAX_FILE:
        raise ValueError("The file exceeds the 1 MiB text limit.")
    if b"\x00" in value:
        raise ValueError("The file is not text.")
    return value.decode("utf-8")


def _write_text(path: Path, content: str, expected: str | None) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE:
        raise ValueError("The file exceeds the 1 MiB text limit.")
    with _parent_descriptor(path, create=expected is None) as parent:
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        except FileNotFoundError:
            if expected is not None:
                raise ValueError("The file changed: expected content no longer exists.")
            mode = 0o644
        else:
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Only regular text files can be written.")
                previous = handle.read(MAX_FILE + 1)
                mode = stat.S_IMODE(info.st_mode)
            if expected is None or previous != expected.encode("utf-8"):
                raise ValueError(
                    "The file changed or expected_content was not provided."
                )
        temporary = f".vikram-edit-{uuid.uuid4().hex}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            # A new-file write must not overwrite a file created concurrently.
            if expected is None:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


class CodingSession:
    def __init__(
        self,
        workspace: str | Path,
        settings: Settings | None = None,
        interaction: Any = None,
        mode: Mode = "normal",
        *,
        max_calls: int = 40,
        max_total_output: int = 256_000,
        on_output: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.workspace = canonical_workspace(workspace)
        if mode not in {"normal", "plan"}:
            raise ValueError("Unknown coding session mode.")
        self.settings = settings or Settings(workspace=self.workspace)
        self.interaction = interaction
        self.mode = mode
        self.max_calls = max_calls
        self.max_total_output = max_total_output
        self.on_output = on_output
        self._count = 0
        self._output = 0
        self._calls: dict[str, tuple[str, asyncio.Future[CodingResult]]] = {}
        self._mutations = asyncio.Lock()
        self._processes: set[asyncio.subprocess.Process] = set()
        self._active: set[asyncio.Task] = set()
        self._closed = False

    @staticmethod
    def tool_definitions() -> list[dict[str, Any]]:
        return [
            {"name": name, "description": spec.description, "parameters": spec.schema}
            for name, spec in TOOL_SPECS.items()
        ]

    def begin_reply(self) -> None:
        """Reset reply budgets without discarding a running or uncertain action."""
        if (
            self._closed
            or self._active
            or any(not pending.done() for _, pending in self._calls.values())
        ):
            raise RuntimeError(
                "Cannot begin a reply while coding actions are pending or closed."
            )
        self._count, self._output = 0, 0
        self._calls.clear()

    def _failure(self, name: str, message: str) -> CodingResult:
        return CodingResult(
            ok=False, tool=name, error=message, activity=f"{name} declined"
        )

    async def dispatch(
        self,
        name: str,
        args: Mapping[str, Any],
        interaction: Any = None,
        mode: Mode | None = None,
        *,
        call_id: str | None = None,
        user_initiated: bool = False,
    ) -> CodingResult:
        if name not in TOOL_SPECS:
            return self._failure(name, "Unknown coding tool.")
        if mode is not None and mode not in {"normal", "plan"}:
            return self._failure(name, "Unknown coding session mode.")
        try:
            arguments = TOOL_SPECS[name].arguments.model_validate(args)
        except ValidationError:
            return self._failure(
                name, "Invalid tool arguments; use the declared schema."
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                [name, arguments.model_dump(), mode or self.mode, user_initiated],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        identifier = call_id or uuid.uuid4().hex
        if identifier in self._calls:
            previous, pending = self._calls[identifier]
            if previous != fingerprint:
                return self._failure(
                    name, "A call ID cannot be reused for a different action."
                )
            return await asyncio.shield(pending)
        if self._closed:
            return self._failure(name, "This coding session is closed.")
        if self._count >= self.max_calls or self._output >= self.max_total_output:
            return self._failure(name, "The shared coding tool budget is exhausted.")
        self._count += 1
        future = asyncio.get_running_loop().create_future()
        self._calls[identifier] = fingerprint, future
        current = asyncio.current_task()
        self._active.add(current)
        try:
            if self._closed:
                raise CodingDenied("This coding session is closed.")
            if not self.settings.is_trusted(self.workspace):
                raise CodingDenied(
                    "Host coding tools require explicit workspace trust."
                )
            if (mode or self.mode) == "plan" and TOOL_SPECS[name].mutation:
                raise CodingDenied(
                    "Plan mode permits inspection only; edits and shell are disabled."
                )
            person = interaction if interaction is not None else self.interaction
            if TOOL_SPECS[name].mutation:
                async with self._mutations:
                    result = await self._execute(
                        name, arguments, person, identifier, user_initiated
                    )
            else:
                result = await self._execute(
                    name, arguments, person, identifier, user_initiated
                )
            remaining = max(0, min(MAX_OUTPUT, self.max_total_output - self._output))
            if len(result.output) + len(result.diff) > remaining:
                result.truncated = True
                result.output = result.output[:remaining]
                result.diff = result.diff[: max(0, remaining - len(result.output))]
            self._output += len(result.output) + len(result.diff)
        except asyncio.CancelledError:
            future.set_result(
                self._failure(
                    name, "The action was cancelled; it will not be replayed."
                )
            )
            raise
        except (CodingDenied, ValueError, OSError) as error:
            message = (
                str(error)
                if isinstance(error, ValueError)
                else "The filesystem operation failed."
            )
            result = self._failure(name, message)
        except Exception:
            result = self._failure(
                name, "The coding action failed; inspect its effects before retrying."
            )
        finally:
            self._active.discard(current)
        future.set_result(result)
        return result

    async def _approve(
        self, person: Any, identifier: str, summary: str, reason: str
    ) -> None:
        if not isinstance(person, Approver):
            raise CodingDenied(
                "This interface cannot approve the requested host action."
            )
        try:
            allowed = await asyncio.wait_for(
                person.approve(
                    ActionRequest(
                        id=identifier,
                        summary=summary,
                        reason=reason,
                        url="",
                        kind="coding",
                    )
                ),
                timeout=APPROVAL_TIMEOUT,
            )
        except TimeoutError:
            allowed = False
        if allowed is not True:
            raise CodingDenied("The user did not approve this host action.")
        if self._closed or not self.settings.is_trusted(self.workspace):
            raise CodingDenied("Workspace trust was revoked before the action ran.")

    async def _path(
        self, raw: str, person: Any, identifier: str, *, write: bool = False
    ) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        lexical = path
        path = path.resolve()
        reasons = []
        if not path.is_relative_to(self.workspace):
            reasons.append("The path is outside the trusted workspace.")
        if _sensitive(path) or _sensitive(lexical):
            reasons.append("The path may contain credentials or trust decisions.")
        if write and (
            ".git" in path.parts or path.name in {"AGENTS.md", "settings.json"}
        ):
            reasons.append(
                "This changes repository metadata or agent instructions/settings."
            )
        if reasons:
            await self._approve(
                person,
                identifier,
                f"{'Write' if write else 'Read'} {path}",
                " ".join(reasons),
            )
        return path

    def _display(self, path: Path) -> str:
        return (
            str(path.relative_to(self.workspace))
            if path.is_relative_to(self.workspace)
            else str(path)
        )

    async def _execute(
        self,
        name: str,
        args: Arguments,
        person: Any,
        identifier: str,
        user_initiated: bool,
    ) -> CodingResult:
        if self._closed or not self.settings.is_trusted(self.workspace):
            raise CodingDenied("Workspace trust was revoked before the action ran.")
        if name == "bash":
            cwd = await self._path(args.cwd, person, identifier)
            if not cwd.is_dir():
                raise ValueError("The command working directory does not exist.")
            reason = _shell_reason(args.command, cwd, self.workspace)
            if reason and not user_initiated:
                await self._approve(person, identifier, f"Run: {args.command}", reason)
            return await self._bash(args, cwd)
        path = await self._path(
            args.path, person, identifier, write=name in {"edit", "write"}
        )
        display = self._display(path)
        if name == "read":
            content = _read_text(path)
            lines = content.splitlines()
            selected = lines[args.offset - 1 : args.offset - 1 + args.limit]
            output = "\n".join(
                f"{i}: {line}" for i, line in enumerate(selected, args.offset)
            )
            return CodingResult(
                ok=True,
                tool=name,
                output=output,
                paths=[display],
                truncated=args.offset - 1 + len(selected) < len(lines),
                activity=f"Read {display}",
            )
        if name in {"edit", "write"}:
            before = _read_text(path) if path.exists() else None
            if name == "edit":
                if before is None or before.count(args.old_text) != 1:
                    raise ValueError(
                        "Exact edit conflict: old_text must occur exactly once."
                    )
                after = before.replace(args.old_text, args.new_text, 1)
                expected = before
            else:
                after, expected = args.content, args.expected_content
            _write_text(path, after, expected)
            diff = "".join(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=display,
                    tofile=display,
                )
            )
            return CodingResult(
                ok=True,
                tool=name,
                output=f"Updated {display}",
                diff=diff,
                paths=[display],
                activity=f"Edited {display}",
            )
        return self._inspect(name, args, path)

    def _inspect(self, name: str, args: Arguments, path: Path) -> CodingResult:
        if not path.is_dir():
            raise ValueError("Choose an existing directory.")
        output, paths, truncated = [], [], False
        seen = 0
        output_size = 0
        if name == "ls":
            entries = (
                (entry, entry.is_dir())
                for entry in sorted(islice(path.iterdir(), MAX_ENTRIES + 1))
            )
        else:

            def walk():
                pending = [path]
                scanned = 0
                while pending:
                    directory = pending.pop()
                    try:
                        with os.scandir(directory) as scan:
                            children = sorted(
                                islice(scan, MAX_ENTRIES + 1 - scanned),
                                key=lambda e: e.name,
                            )
                    except OSError:
                        continue
                    for entry in children:
                        scanned += 1
                        if scanned > MAX_ENTRIES:
                            yield None, False
                            return
                        candidate = Path(entry.path)
                        if (
                            entry.is_symlink()
                            or _sensitive(candidate)
                            or entry.name == ".git"
                        ):
                            continue
                        directory = entry.is_dir(follow_symlinks=False)
                        if directory:
                            pending.append(candidate)
                        yield candidate, directory

            entries = walk()
        for entry, directory in entries:
            seen += 1
            if entry is None or seen > MAX_ENTRIES:
                truncated = True
                break
            relative = str(entry.relative_to(path))
            display = self._display(entry)
            if name == "find" and not (
                fnmatch.fnmatch(relative, args.pattern)
                or fnmatch.fnmatch(entry.name, args.pattern)
            ):
                continue
            if name == "grep":
                if directory or not fnmatch.fnmatch(relative, args.glob):
                    continue
                try:
                    text = _read_text(entry)
                except (OSError, ValueError):
                    continue
                needle = args.pattern.casefold() if args.ignore_case else args.pattern
                for number, line in enumerate(text.splitlines(), 1):
                    if needle not in (line.casefold() if args.ignore_case else line):
                        continue
                    if len(output) >= args.max_results:
                        truncated = True
                        break
                    match = f"{display}:{number}:{line}"
                    remaining = max(0, MAX_OUTPUT - output_size)
                    output.append(match[:remaining])
                    output_size += min(len(match), remaining) + 1
                    truncated |= len(match) > remaining
                    if display not in paths:
                        paths.append(display)
                    if truncated:
                        break
                if truncated:
                    break
                continue
            if len(output) >= args.max_results or output_size >= MAX_OUTPUT:
                truncated = True
                break
            output.append(relative + ("/" if directory else ""))
            output_size += len(output[-1]) + 1
            paths.append(display)
        return CodingResult(
            ok=True,
            tool=name,
            output="\n".join(output),
            paths=paths,
            truncated=truncated,
            activity=f"Inspected {self._display(path)}",
        )

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            pass
        # The shell may exit before its children, including children that ignore TERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def _bash(self, args: BashArgs, cwd: Path) -> CodingResult:
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            args.command,
            cwd=cwd,
            env=child_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self._processes.add(process)
        chunks, size, truncated = [], 0, False

        async def consume():
            nonlocal size, truncated
            while chunk := await process.stdout.read(4096):
                text = chunk.decode("utf-8", errors="replace")
                kept = text[: max(0, MAX_OUTPUT - size)]
                if kept:
                    chunks.append(kept)
                size += len(kept)
                truncated |= len(kept) < len(text)
                if kept and self.on_output:
                    await self.on_output(kept)
            await process.wait()

        error = None
        try:
            await asyncio.wait_for(consume(), timeout=args.timeout)
        except TimeoutError:
            error = "The command timed out and its process group was stopped."
        finally:
            await asyncio.shield(self._stop_process(process))
            self._processes.discard(process)
        return CodingResult(
            ok=error is None and process.returncode == 0,
            tool="bash",
            output="".join(chunks),
            exit_code=process.returncode,
            truncated=truncated,
            error=error,
            activity="Ran host command",
        )

    async def close(self) -> None:
        self._closed = True
        pending = tuple(
            task for task in self._active if task is not asyncio.current_task()
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(
            *(self._stop_process(p) for p in tuple(self._processes)),
            return_exceptions=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
