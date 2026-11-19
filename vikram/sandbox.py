"""Run model-written Python in a local microVM, isolated from this Mac.

Saved conversations reuse a private Pydantic WorkspaceRef. Each reply stops the
VM to flush its disk; only explicit reset destroys it. Temporary replies and
composition still get disposable VMs. All boot from the bundled, digest-pinned
image with no network, mounts, host tools, or host environment.
"""

import asyncio
import dataclasses
import functools
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic_ai.workspaces import Workspace

from vikram.sandbox_session import load_state, save_state, state_lock

if TYPE_CHECKING:
    from pydantic_ai_harness.smol_sandbox import SmolSandboxBackend

IMAGE_REPOSITORY = "library/python"
IMAGE_TAG = "3.13-alpine"
# The linux/arm64 manifest, so the archive's content is fixed and verifiable.
IMAGE_MANIFEST = (
    "sha256:e041b488453556787172f43845c9e0e90b9fb4a91452498420fb2011b23241b6"
)
IMAGE_FILE = "python-3.13-alpine-arm64.tar"
REGISTRY_MANIFEST = "image-manifest.json"
VM_CPUS, VM_MEMORY_MB = 1, 512
# Each of the VM's two disks, sparse files on this Mac. smolvm's default (19.6 GB)
# would let one snippet fill the Mac's disk: the VM writes over 1 GB/s.
VM_DISK_GB = 1
# The free space Vikram always leaves on the Mac's disk.
RESERVE = 20 * 1024**3
RUN_TIMEOUT = 15
# Runs a snippet like the Python REPL: a final bare expression is printed.
RUNNER = """
import ast, sys, traceback
path = sys.argv[1]
try:
    tree = ast.parse(open(path).read(), "<code>")
except SyntaxError as error:
    sys.stderr.write("".join(traceback.format_exception_only(error)))
    sys.exit(1)
tail = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
scope = {"__name__": "__main__"}
try:
    exec(compile(tree, "<code>", "exec"), scope)
    if tail is not None:
        value = eval(compile(ast.Expression(tail.value), "<code>", "eval"), scope)
        if value is not None:
            print(repr(value))
except SystemExit:
    raise
except BaseException as error:
    trace = error.__traceback__.tb_next if error.__traceback__ else None
    sys.stderr.write("".join(traceback.format_exception(type(error), error, trace)))
    sys.exit(1)
"""


class SandboxUnavailable(Exception):
    """The VM could not start; the message is safe to show and to give the model."""


@dataclass(frozen=True)
class CodeRun:
    exit_code: int | None
    output: str
    error: str
    timed_out: bool = False
    notice: str | None = None


class Sandbox(Protocol):
    async def run(self, code: str, /) -> CodeRun: ...

    async def close(self) -> None: ...


def image_path() -> Path | None:
    """The bundled image in an installed app, or the one fetched in a checkout."""
    candidates = [Path(__file__).resolve().parents[1] / "build/sandbox" / IMAGE_FILE]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and isinstance(bundle_root, str):
        candidates.insert(0, Path(bundle_root) / "sandbox" / IMAGE_FILE)
    return next((path for path in candidates if path.is_file()), None)


def verify_archive(path: Path) -> bool:
    """Every file matches the pinned registry manifest, digest by digest."""
    try:
        with tarfile.open(path) as archive:
            files = {}
            for member in archive.getmembers():
                if member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        return False
                    with source:
                        files[member.name] = source.read()
        registry = files[REGISTRY_MANIFEST]
        if f"sha256:{hashlib.sha256(registry).hexdigest()}" != IMAGE_MANIFEST:
            return False
        manifest = json.loads(registry)
        [entry] = json.loads(files["manifest.json"])
        digests = [
            manifest["config"]["digest"],
            *(l["digest"] for l in manifest["layers"]),
        ]
        if [entry["Config"], *entry["Layers"]] != [
            digest.replace(":", "-") for digest in digests
        ]:
            return False
        return all(
            f"sha256:{hashlib.sha256(files[d.replace(':', '-')]).hexdigest()}" == d
            for d in digests
        )
    except (OSError, KeyError, ValueError, TypeError, tarfile.TarError):
        return False


def _pin_guest_rootfs() -> None:
    """Give a frozen app's smolvm guest filesystem a modification time from its content.

    smolvm finds its helper and libraries beside its package, even in a frozen
    app, so nothing is exported to child processes. It unpacks its guest
    filesystem (46 MB) into a cache keyed by the file's size and modification
    time, and a frozen app unpacks its files with new times at every launch, so
    each launch that ran code would leave another copy behind.
    """
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not isinstance(bundle_root, str):
        return
    for name in ("agent-rootfs.tar.zst", "agent-rootfs.tar"):
        path = Path(bundle_root) / "smol" / name
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).digest()
            stamp = 1_000_000_000 + int.from_bytes(digest[:4]) % 10**9
            os.utime(path, (stamp, stamp))
            return


@functools.cache
def unavailable_reason() -> str | None:
    """Why this host cannot run the sandbox, or `None` when it can."""
    if not (sys.platform == "darwin" and platform.machine() == "arm64"):
        return "The Python sandbox needs an Apple silicon Mac."
    # Before smolvm first loads, in case it reads the time when configured.
    _pin_guest_rootfs()
    path = image_path()
    if path is None:
        return (
            "The Python sandbox image is missing. Rebuild Vikram, or run "
            "`uv run python scripts/fetch_sandbox_image.py` in a checkout."
        )
    if not verify_archive(path):
        return "The Python sandbox image is damaged. Rebuild or reinstall Vikram."
    try:
        import smol

        available, code, _ = smol.local_availability()
    except ImportError:
        return "The Python sandbox runtime is not installed."
    except Exception as error:
        available, code = False, type(error).__name__
    if not available:
        return f"This Mac cannot start sandbox VMs ({code})."
    return None


def _markers() -> Path:
    # Disposable orphans may be swept; persistent disks belong to private chat refs.
    return Path.home() / "Library/Caches/dev.raniendu.vikram/sandbox-vms"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_swept = False


async def _sweep() -> None:
    """Delete disposable VMs left by a crashed process; never conversation disks."""
    global _swept
    if _swept:
        return
    _swept = True
    from pydantic_ai.workspaces import WorkspaceRef
    from pydantic_ai_harness.smol_sandbox import SmolSandbox

    for marker in _markers().glob("*.json"):
        try:
            record = json.loads(marker.read_text())
            if not isinstance(record, dict):
                raise ValueError("Invalid sandbox process marker.")
            if record.get("persistent") is True:
                continue  # Conversation-owned disks are never crash garbage.
            pid = record["pid"]
            if type(pid) is not int or pid <= 0:
                raise ValueError("Invalid sandbox process marker.")
            ref = WorkspaceRef(provider="smol", id=marker.stem)
        except (OSError, ValueError, KeyError, TypeError):
            marker.unlink(missing_ok=True)  # Unreadable: there is nothing to delete.
            continue
        if _alive(pid):
            continue
        try:
            # A VM that is already gone returns quietly.
            await SmolSandbox().destroy(ref)
        except ValueError:
            pass  # Not a VM name Vikram creates.
        except Exception:
            continue  # Keep the marker and try again next time.
        marker.unlink(missing_ok=True)


def _describe(error: BaseException) -> str:
    # smolvm's error code when there is one; never a message, which can hold paths.
    for cause in (error, error.__cause__):
        if isinstance(code := getattr(cause, "code", None), str) and code:
            return code
    return type(error).__name__


class VmSandbox:
    """A bounded Smol workspace, optionally persisted in a private chat library."""

    def __init__(self, *, state_path: Path | None = None) -> None:
        self.state_path = state_path
        self._backend: SmolSandboxBackend | None = None
        self._workspace: Workspace | None = None
        self._broken: str | None = None
        self._boot: asyncio.Task | None = None
        self._closing: asyncio.Task[None] | None = None
        self._lease: AbstractContextManager | None = None
        self._persisted = False
        self._notice: str | None = None
        self._lock = asyncio.Lock()

    async def _start(self) -> Workspace:
        from pydantic_ai.workspaces import Workspace
        from pydantic_ai_harness.smol_sandbox import SmolSandboxBackend

        if shutil.disk_usage(Path.home()).free - 2 * VM_DISK_GB * 1024**3 < RESERVE:
            raise SandboxUnavailable(
                "The Python sandbox needs more free disk space to keep the 20 GiB reserve."
            )
        boot = None
        marked = False
        try:
            stored = None
            if self.state_path is not None:
                self._lease = state_lock(self.state_path)
                self._lease.__enter__()
                stored = load_state(self.state_path)
                self._persisted = stored is not None
                if stored is not None and not stored.saved:
                    self._notice = (
                        "The previous sandbox shutdown was interrupted. Recent writes "
                        "may be missing. Inspect the current files before continuing; "
                        "do not replay earlier effects automatically."
                    )
            await _sweep()
            backend = SmolSandboxBackend(
                ref=stored.ref if stored else None,
                image=str(image_path()),
                cpus=VM_CPUS,
                memory_mb=VM_MEMORY_MB,
            )
            resources = getattr(backend, "_resources", None)
            if (
                not dataclasses.is_dataclass(resources)
                or isinstance(resources, type)
                or not hasattr(resources, "storage_gb")
            ):
                raise SandboxUnavailable(
                    "The Python sandbox cannot limit its disk size."
                )
            backend._resources = dataclasses.replace(
                backend._resources, storage_gb=VM_DISK_GB, overlay_gb=VM_DISK_GB
            )
            self._backend = backend
            workspace = Workspace(backend)
            boot = self._boot = asyncio.ensure_future(workspace.working_dir())
            await asyncio.sleep(0)  # The native ref exists before boot finishes.
            self._mark()
            marked = True
            await boot
        except BaseException as error:
            if not marked or isinstance(error, asyncio.CancelledError):
                with suppress(Exception):
                    await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            self._broken = (
                str(error)
                if isinstance(error, (SandboxUnavailable, ValueError))
                else f"The Python sandbox could not start ({_describe(error)})."
            )
            if self._persisted:
                self._broken += (
                    " The saved sandbox was not recreated. Inspect or explicitly "
                    "reset it in Workbench or with `vikram sandbox reset`."
                )
            raise SandboxUnavailable(self._broken) from None
        finally:
            self._boot = None
        self._workspace = workspace
        return workspace

    def _mark(self) -> None:
        ref = self._backend.ref if self._backend else None
        if ref is None:
            raise SandboxUnavailable(
                "The Python sandbox did not provide a workspace reference."
            )
        if self.state_path is not None:
            save_state(self.state_path, ref, saved=False)
            self._persisted = True
        _markers().mkdir(parents=True, exist_ok=True)
        (_markers() / f"{ref.id}.json").write_text(
            json.dumps({"pid": os.getpid(), "persistent": self._persisted})
        )

    async def run(self, code: str, /) -> CodeRun:
        async with self._lock:
            return await self._run(code)

    async def _run(self, code: str) -> CodeRun:
        from pydantic_ai.workspaces import (
            WorkspaceOutputLimitError,
            WorkspaceTimeoutError,
            WorkspaceUnavailableError,
        )

        if self._broken:
            raise SandboxUnavailable(self._broken)
        workspace = self._workspace or await self._start()
        try:
            await workspace.write_text("snippet.py", code)
            result = await workspace.run(
                ["python", "-c", RUNNER, "snippet.py"], timeout=RUN_TIMEOUT
            )
        except WorkspaceTimeoutError:
            return CodeRun(None, "", f"Stopped after {RUN_TIMEOUT} seconds.", True)
        except WorkspaceOutputLimitError:
            return CodeRun(None, "", "Stopped: the output exceeded 10 MiB.")
        except WorkspaceUnavailableError:
            self._broken = "The Python sandbox stopped unexpectedly."
            raise SandboxUnavailable(self._broken) from None
        notice, self._notice = self._notice, None
        return CodeRun(result.exit_code, result.stdout, result.stderr, notice=notice)

    async def close(self) -> None:
        """Drain one shared shutdown even if its callers are cancelled repeatedly."""
        closing = self._closing
        if closing is None:
            closing = self._closing = asyncio.create_task(self._close())
        cancelled = False
        try:
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
        finally:
            if self._closing is closing:
                self._closing = None
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        """Stop/flush a saved workspace; destroy only an ephemeral or unsaved VM."""
        boot, self._boot = self._boot, None
        if boot is not None and not boot.done():
            boot.cancel()
            await asyncio.gather(boot, return_exceptions=True)
        backend, self._backend, self._workspace = self._backend, None, None
        try:
            if backend is None or backend.ref is None:
                return
            ref = backend.ref
            if self.state_path is not None and self._persisted:
                try:
                    machine = await backend.get_sandbox()
                    await machine.stop()  # Native Smol persistence requires a flush.
                    save_state(self.state_path, ref, saved=self._broken is None)
                except Exception:
                    raise SandboxUnavailable(
                        "The conversation sandbox could not be safely saved. Its reference "
                        "was retained, but recent writes are uncertain. Inspect it before continuing."
                    ) from None
            else:
                from pydantic_ai_harness.smol_sandbox import SmolSandbox

                try:
                    await SmolSandbox().destroy(ref)
                except Exception:
                    return  # Leave the crash marker for the next sweep.
                with suppress(OSError):
                    (_markers() / f"{ref.id}.json").unlink(missing_ok=True)
        finally:
            lease, self._lease = self._lease, None
            if lease is not None:
                lease.__exit__(None, None, None)


COMPOSITION_FRAME_LIMIT = 1024 * 1024
# This control program runs in the guest through the existing VM control API.
# It never evaluates request contents and never reads more than one bounded batch.
_POLL_COMPOSITION = r"""
import json, os, re, stat, sys
root, maximum = sys.argv[1], int(sys.argv[2])
frames, total = [], 2
with os.scandir(root) as scan:
    for entry in scan:
        if len(frames) >= 5:
            break
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}\.request", entry.name):
            continue
        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Invalid broker frame")
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Broker frame exceeds limit")
        if frames and total + len(data) > maximum - 100:
            break
        frame = json.loads(data)
        if not isinstance(frame, dict) or frame.get("id") != entry.name[:-8]:
            raise ValueError("Invalid broker frame identity")
        frames.append(frame)
        total += len(data) + 2
        os.replace(entry.path, entry.path + ".claimed")
print(json.dumps(frames, separators=(",", ":")))
"""


class CompositionSandbox(VmSandbox):
    """Separate per-composition VM with bounded JSON control files, never mounts."""

    async def prepare(self, code: str, tools: list[dict], execution_id: str) -> None:
        from vikram.composition import GUEST_RUNNER

        if not execution_id.isalnum() or len(execution_id) > 64:
            raise ValueError("Invalid composition identifier.")
        reason = unavailable_reason()
        if reason:
            raise SandboxUnavailable(reason)
        workspace = self._workspace or await self._start()
        self._control = f"composition/{execution_id}"
        await workspace.write_text(f"{self._control}/code.py", code)
        await workspace.write_text(f"{self._control}/tools.json", json.dumps(tools))
        await workspace.write_text(f"{self._control}/runner.py", GUEST_RUNNER)

    async def run_composition(self) -> CodeRun:
        from pydantic_ai.workspaces import (
            WorkspaceOutputLimitError,
            WorkspaceTimeoutError,
        )

        workspace = self._workspace
        if workspace is None:
            raise SandboxUnavailable("The composition workspace is not prepared.")
        try:
            result = await workspace.run(
                ["python", f"{self._control}/runner.py", self._control], timeout=30 * 60
            )
        except WorkspaceTimeoutError:
            return CodeRun(None, "", "Composition exceeded its overall deadline.", True)
        except WorkspaceOutputLimitError:
            return CodeRun(None, "", "Composition exceeded the VM output limit.")
        return CodeRun(result.exit_code, result.stdout, result.stderr)

    async def poll_requests(self) -> list[dict]:
        workspace = self._workspace
        if workspace is None:
            raise SandboxUnavailable("The composition workspace is not prepared.")
        result = await workspace.run(
            [
                "python",
                "-c",
                _POLL_COMPOSITION,
                self._control,
                str(COMPOSITION_FRAME_LIMIT),
            ],
            timeout=5,
        )
        if result.exit_code or len(result.stdout.encode()) > COMPOSITION_FRAME_LIMIT:
            raise ValueError(
                "The composition broker received an invalid or oversized frame."
            )
        try:
            frames = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("The composition broker received invalid JSON.") from None
        if not isinstance(frames, list) or len(frames) > 5:
            raise ValueError("The composition broker received an invalid batch.")
        return frames

    async def write_response(self, identifier: str, result: dict) -> None:
        if (
            not identifier
            or len(identifier) > 100
            or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in identifier)
        ):
            raise ValueError("Invalid composition call identifier.")
        encoded = json.dumps(result, allow_nan=False)
        if len(encoded.encode()) > COMPOSITION_FRAME_LIMIT:
            raise ValueError("The composition result exceeds the frame limit.")
        # Workspace writes publish atomically through its guest filesystem API.
        workspace = self._workspace
        if workspace is None:
            raise SandboxUnavailable("The composition workspace is not prepared.")
        await workspace.write_text(f"{self._control}/{identifier}.response", encoded)


def host_sandbox(*, state_path: Path | None = None) -> Sandbox | None:
    """A lazy saved workspace, or a disposable VM for a temporary reply."""
    return VmSandbox(state_path=state_path) if unavailable_reason() is None else None


def check_sandbox() -> int:
    """Boot a VM and run one snippet without a model, to check a packaged runtime."""
    reason = unavailable_reason()
    if reason is None:

        async def probe() -> CodeRun:
            sandbox = VmSandbox()
            try:
                return await sandbox.run("sum(range(10))")
            finally:
                await sandbox.close()

        try:
            run = asyncio.run(probe())
            if run.output.strip() != "45":
                reason = "The Python sandbox returned an unexpected result."
        except SandboxUnavailable as error:
            reason = str(error)
    if reason is not None:
        sys.stderr.write(f"{reason}\n")
        return 1
    sys.stdout.write("The Python sandbox is ready.\n")
    return 0
