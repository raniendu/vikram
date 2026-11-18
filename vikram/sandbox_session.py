"""Private, conversation-owned Pydantic workspace references, never transcript input."""

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.workspaces import WorkspaceRef

MAX_STATE = 4096


@dataclass(frozen=True)
class SavedSandbox:
    ref: WorkspaceRef
    saved: bool


def state_path(root: Path, chat_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", chat_id) is None:
        raise ValueError("Invalid conversation identifier for a sandbox.")
    return root / "sandboxes" / f"{chat_id}.json"


def load_state(path: Path) -> SavedSandbox | None:
    """Read only the private library's ref; imports and model output never supply it."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as source:
            data = source.read(MAX_STATE + 1)
        if len(data) > MAX_STATE:
            raise ValueError
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("provider") != "smol"
            or not isinstance(value.get("id"), str)
            or re.fullmatch(r"pydantic-ai-[0-9a-f]{32}", value["id"]) is None
            or type(value.get("saved")) is not bool
        ):
            raise ValueError
        return SavedSandbox(
            WorkspaceRef(provider="smol", id=value["id"]), value["saved"]
        )
    except (ValueError, TypeError, KeyError):
        raise ValueError(
            "The saved sandbox reference is invalid. It was not attached or replaced. "
            "Recover its private metadata before continuing."
        ) from None


def save_state(path: Path, ref: WorkspaceRef, *, saved: bool) -> None:
    if (
        ref.provider != "smol"
        or re.fullmatch(r"pydantic-ai-[0-9a-f]{32}", ref.id) is None
    ):
        raise ValueError("Refusing a sandbox reference outside Pydantic's namespace.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".sandbox-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(
                {"version": 1, "provider": ref.provider, "id": ref.id, "saved": saved},
                target,
            )
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def state_lock(path: Path):
    """Serialize attachment/reset even across independent library controllers."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(
        path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "This conversation's sandbox is in use. Stop its reply first."
            ) from None
        yield
    finally:
        os.close(descriptor)


def sandbox_status(path: Path) -> dict:
    try:
        state = load_state(path)
    except (OSError, ValueError):
        return {"state": "unavailable", "persistent": True}
    return {
        "state": (
            "not_created"
            if state is None
            else "saved" if state.saved else "interrupted"
        ),
        "persistent": True,
    }


async def reset_sandbox(path: Path) -> None:
    """Explicit user operation only. Preserve the ref when deletion fails."""
    with state_lock(path):
        state = load_state(path)
        if state is None:
            return
        from pydantic_ai_harness.smol_sandbox import SmolSandbox

        await SmolSandbox().destroy(state.ref)
        path.unlink()
        from vikram.sandbox import _markers

        (_markers() / f"{state.ref.id}.json").unlink(missing_ok=True)
