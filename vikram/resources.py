"""Trusted, bounded project instructions and declarative resources; no code loading."""

import json
import os
import re
import stat
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from vikram.settings import Settings

MAX_RESOURCE_BYTES = 64 * 1024
MAX_RESOURCES = 100
MAX_INSTRUCTIONS = 128 * 1024
KEY_ACTIONS = frozenset(
    {
        "send",
        "newline",
        "cancel",
        "models",
        "thinking",
        "tools",
        "search",
        "editor",
        "queue",
        "copy",
        "clear",
        "quit",
        "complete",
    }
)


@dataclass(frozen=True)
class Resource:
    name: str
    description: str
    path: Path
    source: str


@dataclass(frozen=True)
class ResourceCatalog:
    skills: tuple[Resource, ...] = ()
    templates: tuple[Resource, ...] = ()
    themes: tuple[Resource, ...] = ()
    keybindings: dict[str, str] | None = None


def _text(path: Path, boundary: Path, *, metadata: bool = False) -> str:
    try:
        if not path.resolve().is_relative_to(boundary.resolve()):
            return ""
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return ""
            raw = handle.read(8192 if metadata else MAX_RESOURCE_BYTES + 1)
        return raw.decode("utf-8") if len(raw) <= MAX_RESOURCE_BYTES else ""
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return ""


def _metadata(text: str, fallback: str) -> tuple[str, str]:
    """Read only scalar name/description front matter, without YAML constructors."""
    values = {}
    if text.startswith("---\n"):
        header, separator, _ = text[4:].partition("\n---")
        if separator and len(header) <= 8192:
            for line in header.splitlines():
                key, colon, value = line.partition(":")
                if colon and key in {"name", "description"}:
                    values[key] = value.strip().strip("\"'")[:1000]
    name = values.get("name", fallback)
    if not re.fullmatch(r"[\w.-]{1,100}", name):
        name = fallback
    return name, values.get("description", "")


class ProjectResources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog = ResourceCatalog()

    def _roots(self) -> list[tuple[Path, str]]:
        roots = [(self.settings.root, "user")]
        if self.settings.workspace and self.settings.is_trusted():
            roots.append((self.settings.workspace / ".vikram", "project"))
            roots.append((self.settings.workspace / ".agents", "project"))
        return roots

    def instructions(self, path: str | Path | None = None) -> str:
        pieces = []
        user = _text(self.settings.root / "AGENTS.md", self.settings.root)
        if user:
            pieces.append(user)
        workspace = self.settings.workspace
        if workspace and self.settings.is_trusted():
            target = Path(path or workspace)
            if not target.is_absolute():
                target = workspace / target
            target = target.resolve()
            if target.is_relative_to(workspace):
                if target.is_file():
                    target = target.parent
                directories = [workspace]
                directories.extend(
                    reversed(
                        [
                            p
                            for p in [target, *target.parents]
                            if p != workspace and p.is_relative_to(workspace)
                        ]
                    )
                )
                for directory in directories:
                    text = _text(directory / "AGENTS.md", workspace)
                    if text:
                        pieces.append(text)
        return "\n\n".join(pieces)[:MAX_INSTRUCTIONS]

    def reload(self) -> ResourceCatalog:
        kinds: dict[str, dict[str, Resource]] = {
            "skills": {},
            "templates": {},
            "themes": {},
        }
        keybindings = {}
        for root, source in self._roots():
            boundary = self.settings.workspace if source == "project" else root
            for kind, pattern in (
                ("skills", "*/SKILL.md"),
                ("templates", "*.md"),
                ("themes", "*.json"),
            ):
                for path in sorted(islice((root / kind).glob(pattern), MAX_RESOURCES)):
                    text = _text(path, boundary, metadata=kind == "skills")
                    if not text:
                        continue
                    name, description = _metadata(
                        text, path.parent.name if kind == "skills" else path.stem
                    )
                    kinds[kind][name] = Resource(name, description, path, source)
            try:
                raw = json.loads(_text(root / "keybindings.json", boundary) or "{}")
                if isinstance(raw, dict):
                    keybindings.update(
                        (key, action)
                        for key, action in raw.items()
                        if isinstance(key, str)
                        and len(key) <= 40
                        and isinstance(action, str)
                        and action in KEY_ACTIONS
                    )
            except ValueError:
                pass
        self.catalog = ResourceCatalog(
            *(
                tuple(kinds[kind].values())[:MAX_RESOURCES]
                for kind in ("skills", "templates", "themes")
            ),
            keybindings=keybindings,
        )
        return self.catalog

    def _read(self, kind: str, name: str) -> str:
        self.reload()
        resource = next(
            (r for r in getattr(self.catalog, kind) if r.name == name), None
        )
        if resource is None:
            raise ValueError(f"Unknown {kind} resource.")
        boundary = (
            self.settings.workspace
            if resource.source == "project"
            else self.settings.root
        )
        return _text(resource.path, boundary)

    def read_skill(self, name: str) -> str:
        return self._read("skills", name)

    def template(self, name: str) -> str:
        return self._read("templates", name)

    def theme(self, name: str) -> dict[str, str]:
        try:
            value = json.loads(self._read("themes", name))
        except ValueError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            k: v
            for k, v in value.items()
            if isinstance(k, str)
            and re.fullmatch(r"[\w.-]{1,80}", k)
            and isinstance(v, str)
            and len(v) <= 100
            and re.fullmatch(r"[\w# .:-]+", v)
        }
