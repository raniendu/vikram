"""Bounded declarative preferences and explicit, canonical project trust.

Project files never establish trust, select cloud inference, or carry credentials.
Only an interface handling a person's explicit choice calls ``trust`` or ``set``.
"""

import copy
import fcntl
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

MAX_SETTINGS_BYTES = 64 * 1024
DEFAULTS: dict[str, Any] = {
    "model": None,
    "thinking": "on",
    "theme": "system",
    "interface": "fullscreen",
    "show_thinking": False,
    "show_tools": False,
    "auto_compact": True,
    "models": [],
    "keybindings": {},
}
PROJECT_KEYS = frozenset(DEFAULTS)
USER_KEYS = PROJECT_KEYS | {"editor", "context_limit", "mcp_servers", "extensions"}


def application_root() -> Path:
    return Path.home() / "Library/Application Support/dev.raniendu.vikram"


def canonical_workspace(workspace: str | Path) -> Path:
    path = Path(workspace).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("The workspace must be an existing directory.")
    return path


def read_json(path: Path) -> dict[str, Any]:
    """Malformed/oversized preferences fail closed without leaking their contents."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) > MAX_SETTINGS_BYTES:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid(key: str, value: Any, *, project: bool = False) -> bool:
    if key not in (PROJECT_KEYS if project else USER_KEYS):
        return False
    if key == "model":
        if value is None:
            return True
        if not isinstance(value, str) or len(value) > 300:
            return False
        from vikram.providers import ModelRef

        try:
            reference = ModelRef.parse(value)
        except ValueError:
            return False
        return value == value.strip() and (not project or not reference.is_cloud)
    if key == "models":
        return (
            isinstance(value, list)
            and len(value) <= 100
            and all(
                isinstance(item, str) and _valid("model", item, project=project)
                for item in value
            )
        )
    if key in {"show_thinking", "show_tools", "auto_compact"}:
        return isinstance(value, bool)
    if key == "interface":
        return isinstance(value, str) and value in {"fullscreen", "regular", "plain"}
    if key == "context_limit":
        return value is None or (
            isinstance(value, int) and not isinstance(value, bool) and value > 0
        )
    if key in {"keybindings", "mcp_servers"}:
        return isinstance(value, dict) and not _contains_credentials(value)
    if key == "extensions":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, str) and len(value) <= 1000


def _contains_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            not isinstance(key, str)
            or key.lower().replace("-", "_")
            in {
                "api_key",
                "apikey",
                "password",
                "token",
                "access_token",
                "refresh_token",
                "client_secret",
                "authorization",
                "secret",
            }
            or _contains_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_credentials(item) for item in value)
    return False


class Settings:
    def __init__(
        self,
        root: str | Path | None = None,
        workspace: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve() if root else application_root()
        self.workspace = canonical_workspace(workspace) if workspace else None
        self.overrides = dict(overrides or {})

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.root / ".settings.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _save(path: Path, values: Mapping[str, Any]) -> None:
        encoded = json.dumps(values, indent=2, ensure_ascii=False) + "\n"
        if len(encoded.encode()) > MAX_SETTINGS_BYTES:
            raise ValueError("Settings exceed the size limit.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _workspace(self, workspace: str | Path | None = None) -> Path:
        if workspace is None and self.workspace is None:
            raise ValueError("No project workspace was selected.")
        return canonical_workspace(workspace or self.workspace)

    def is_trusted(self, workspace: str | Path | None = None) -> bool:
        try:
            selected = self._workspace(workspace)
        except (ValueError, OSError):
            return False
        return read_json(self.root / "trust.json").get(str(selected)) is True

    def trust(self, workspace: str | Path | None = None) -> None:
        selected = self._workspace(workspace)
        with self._locked():
            trusted = read_json(self.root / "trust.json")
            trusted[str(selected)] = True
            self._save(self.root / "trust.json", trusted)

    def untrust(self, workspace: str | Path | None = None) -> None:
        selected = self._workspace(workspace)
        with self._locked():
            trusted = read_json(self.root / "trust.json")
            trusted.pop(str(selected), None)
            self._save(self.root / "trust.json", trusted)

    def load(self, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = copy.deepcopy(DEFAULTS)
        values.update(
            (k, v)
            for k, v in read_json(self.root / "settings.json").items()
            if _valid(k, v)
        )
        if self.workspace and self.is_trusted():
            project = self.workspace / ".vikram" / "settings.json"
            if project.resolve().is_relative_to(self.workspace):
                values.update(
                    (k, v)
                    for k, v in read_json(project).items()
                    if _valid(k, v, project=True)
                )
        values.update((k, v) for k, v in self.overrides.items() if _valid(k, v))
        values.update((k, v) for k, v in (overrides or {}).items() if _valid(k, v))
        return values

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(
        self, key: str, value: Any, *, scope: Literal["user", "project"] = "user"
    ) -> None:
        if scope not in {"user", "project"}:
            raise ValueError("Choose user or project settings.")
        if not _valid(key, value, project=scope == "project"):
            raise ValueError(
                "Unsupported setting or value; credentials are never settings."
            )
        if scope == "project":
            if not self.is_trusted():
                raise ValueError("Project settings require explicit workspace trust.")
            path = self._workspace() / ".vikram" / "settings.json"
            if not path.resolve().is_relative_to(self._workspace()):
                raise ValueError("Project settings must remain inside the workspace.")
        else:
            path = self.root / "settings.json"
        with self._locked():
            values = read_json(path)
            values[key] = value
            self._save(path, values)
