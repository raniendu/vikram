"""Tiered, config-driven policy for shell-command execution.

The engine classifies a parsed command (argv + raw string) into one of three
tiers:

* ``auto``    — read-only; run with no approval (Tier 1).
* ``approve`` — the default; requires human-in-the-loop approval (Tier 2).
* ``deny``    — catastrophic/irreversible; refused even if approved (Tier 3).

Policy lives in a declarative TOML file (``spec/shared/command_policy.toml``)
so widening or narrowing it is a reviewed config edit rather than a code
change. ``tools.py`` loads a :class:`CommandPolicy` and calls
:meth:`CommandPolicy.classify`.
"""

from __future__ import annotations

import copy
import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

POLICY_FILENAME = "command_policy.toml"

Decision = Literal["auto", "approve", "deny"]


class CommandPolicyError(RuntimeError):
    """Raised when a command policy file is missing or invalid."""


@dataclass(frozen=True)
class GitReadOnlyRules:
    subcommands: frozenset[str]
    forbidden_flag_substrings: tuple[str, ...]
    mutating_subcommand_args: frozenset[str]


@dataclass(frozen=True)
class ReadOnlyRules:
    always: frozenset[str]
    git: GitReadOnlyRules | None


@dataclass(frozen=True)
class DenyRule:
    name: str
    message: str
    executables: frozenset[str]  # basenames; empty => executable-agnostic
    subcommand: str | None
    any_flag: tuple[str, ...]  # exact-token match
    any_flag_prefixes: tuple[str, ...]  # token.startswith match (e.g. ":" refspec)
    path_globs: tuple[str, ...]
    exclude_path_globs: tuple[str, ...]
    raw_substrings: tuple[str, ...]

    def matches(self, argv: list[str], raw: str) -> bool:
        if self.raw_substrings and any(sub in raw for sub in self.raw_substrings):
            return True
        if self.path_globs and self._matches_path(argv):
            return True
        if not self.executables or not argv:
            return False
        if Path(argv[0]).name not in self.executables:
            return False
        rest = argv[1:]
        if self.subcommand is not None and (not rest or rest[0] != self.subcommand):
            return False
        if self.any_flag or self.any_flag_prefixes:
            flag_match = any(flag in rest for flag in self.any_flag) or any(
                token.startswith(prefix)
                for token in rest
                for prefix in self.any_flag_prefixes
            )
            if not flag_match:
                return False
        return True

    def _matches_path(self, argv: list[str]) -> bool:
        for token in argv:
            base = Path(token).name
            if any(
                fnmatch.fnmatch(token, glob) or fnmatch.fnmatch(base, glob)
                for glob in self.exclude_path_globs
            ):
                continue
            if any(
                fnmatch.fnmatch(token, glob) or fnmatch.fnmatch(base, glob)
                for glob in self.path_globs
            ):
                return True
        return False


@dataclass(frozen=True)
class CommandPolicy:
    read_only: ReadOnlyRules
    deny: tuple[DenyRule, ...]

    def classify(self, argv: list[str], raw: str) -> tuple[Decision, str | None]:
        """Classify a command. Deny wins, then read-only, else approve."""
        for rule in self.deny:
            if rule.matches(argv, raw):
                return "deny", rule.message
        if self._is_read_only(argv):
            return "auto", None
        return "approve", None

    def _is_read_only(self, argv: list[str]) -> bool:
        if not argv:
            return False
        name = Path(argv[0]).name
        if name in self.read_only.always:
            return True
        if self.read_only.git is not None and name == "git":
            return _is_read_only_git(argv[1:], self.read_only.git)
        return False


def _token_is_forbidden(token: str, forbidden: tuple[str, ...]) -> bool:
    return any(token == flag or token.startswith(flag) for flag in forbidden)


def _is_read_only_git(args: list[str], rules: GitReadOnlyRules) -> bool:
    if not args:
        return False
    subcommand, rest = args[0], args[1:]
    if subcommand not in rules.subcommands:
        return False
    if any(
        _token_is_forbidden(token, rules.forbidden_flag_substrings) for token in rest
    ):
        return False
    if subcommand in rules.mutating_subcommand_args and any(
        not token.startswith("-") for token in rest
    ):
        return False
    return True


def _build_deny_rule(raw: dict[str, Any]) -> DenyRule:
    executables = raw.get("executable", [])
    if isinstance(executables, str):
        executables = [executables]
    return DenyRule(
        name=str(raw.get("name", "unnamed")),
        message=str(raw.get("message", "This command is not allowed.")),
        executables=frozenset(executables),
        subcommand=raw.get("subcommand"),
        any_flag=tuple(raw.get("any_flag", [])),
        any_flag_prefixes=tuple(raw.get("any_flag_prefixes", [])),
        path_globs=tuple(raw.get("path_globs", [])),
        exclude_path_globs=tuple(raw.get("exclude_path_globs", [])),
        raw_substrings=tuple(raw.get("raw_substrings", [])),
    )


def build_policy(data: dict[str, Any]) -> CommandPolicy:
    """Build a :class:`CommandPolicy` from a parsed TOML mapping."""
    read_only_raw = data.get("read_only", {})
    git_raw = read_only_raw.get("git")
    git_rules = None
    if git_raw is not None:
        git_rules = GitReadOnlyRules(
            subcommands=frozenset(git_raw.get("subcommands", [])),
            forbidden_flag_substrings=tuple(
                git_raw.get("forbidden_flag_substrings", [])
            ),
            mutating_subcommand_args=frozenset(
                git_raw.get("mutating_subcommand_args", [])
            ),
        )
    read_only = ReadOnlyRules(
        always=frozenset(read_only_raw.get("always", [])),
        git=git_rules,
    )
    deny_raw = data.get("deny", {}).get("rule", [])
    deny = tuple(_build_deny_rule(rule) for rule in deny_raw)
    return CommandPolicy(read_only=read_only, deny=deny)


def merge_policy_data(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge a per-agent override over the shared base.

    ``read_only`` leaves are replaced; ``deny.rule`` is append-only so an
    agent override can never remove a shared (catastrophic) deny rule.
    """
    merged = copy.deepcopy(base)
    if "read_only" in override:
        base_read_only = merged.setdefault("read_only", {})
        for key, value in override["read_only"].items():
            if (
                key == "git"
                and isinstance(value, dict)
                and isinstance(base_read_only.get("git"), dict)
            ):
                base_read_only["git"] = {**base_read_only["git"], **value}
            else:
                base_read_only[key] = value
    if "deny" in override:
        extra_rules = list(override["deny"].get("rule", []))
        deny = merged.setdefault("deny", {})
        deny["rule"] = list(deny.get("rule", [])) + extra_rules
    return merged


def load_policy_data(path: Path) -> dict[str, Any]:
    """Parse a command policy TOML file into a raw mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CommandPolicyError(f"Command policy file not found: {path}") from exc
    except OSError as exc:
        raise CommandPolicyError(
            f"Could not read command policy {path}: {exc}"
        ) from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CommandPolicyError(
            f"Could not parse command policy {path}: {exc}"
        ) from exc


def load_command_policy(
    path: Path, override: dict[str, Any] | None = None
) -> CommandPolicy:
    """Load and build a :class:`CommandPolicy`, applying an optional override."""
    data = load_policy_data(path)
    if override:
        data = merge_policy_data(data, override)
    return build_policy(data)
