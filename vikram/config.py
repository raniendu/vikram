"""Local model configuration: ``~/.config/vikram/config.toml``.

Schema v2 stores one section per provider plus a default selection::

    config_version = 2
    default_provider = "anthropic"

    [providers.anthropic]
    model = "claude-sonnet-5"
    api_key = "sk-ant-..."

    [providers.ollama]
    model = "llama3.2"
    base_url = "http://localhost:11434"

v1 flat files (``model_provider`` / ``model`` / ``*_api_key`` keys) are
migrated in memory on every load and rewritten as v2 the next time
``vikram configure`` runs. Writes always merge into the existing file so
re-running the wizard (or an installer update) never discards providers
configured earlier.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from vikram.providers import PROVIDER_IDS, PROVIDERS, Provider

CONFIG_FILE_NAME = "config.toml"
CONFIG_VERSION = 2

ENV_KEY_MAP = {
    "VIKRAM_MODEL_PROVIDER": "model_provider",
    "VIKRAM_MODEL": "model",
    "OLLAMA_BASE_URL": "ollama_base_url",
    "VIKRAM_OPENAI_COMPAT_API_KEY": "openai_compat_api_key",
    "OPENAI_API_KEY": "openai_compat_api_key",
    "DIGITALOCEAN_ACCESS_TOKEN": "openai_compat_api_key",
    "SARVAM_API_KEY": "openai_compat_api_key",
    "VIKRAM_OPENAI_COMPAT_BASE_URL": "openai_compat_base_url",
}

_FILE_HEADER = "# Written by `vikram configure`.\n"
_TOP_LEVEL_KEY_ORDER = ("config_version", "default_provider", "model")
_PROVIDER_SECTION_KEY_ORDER = ("provider", "model", "base_url", "api_key")


class ConfigParseError(RuntimeError):
    """Raised when config.toml exists but cannot be parsed as TOML."""


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "vikram"


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def _normalize_key(key: str) -> str:
    return ENV_KEY_MAP.get(key.upper(), key)


def load_config_raw(path: Path | None = None) -> dict[str, Any]:
    """Parse config.toml as-is. Empty dict when missing; raises on bad TOML."""
    path = path or config_path()
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigParseError(f"Could not parse {path}: {exc}") from exc


def migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Reshape a v1 flat config into the v2 nested layout (pure, in-memory)."""
    if not data:
        return {}
    if isinstance(data.get("providers"), dict) or data.get("config_version"):
        return data

    migrated: dict[str, Any] = {}
    providers: dict[str, dict[str, Any]] = {}
    provider: str | None = None
    model: str | None = None
    for raw_key, value in data.items():
        key = _normalize_key(str(raw_key))
        if value in (None, ""):
            continue
        if key == "model_provider":
            provider = str(value)
        elif key == "model":
            model = str(value)
        elif key == "ollama_base_url":
            providers.setdefault("ollama", {})["base_url"] = value
        elif key == "openai_compat_api_key":
            providers.setdefault("openai-compatible", {})["api_key"] = value
        elif key == "openai_compat_base_url":
            providers.setdefault("openai-compatible", {})["base_url"] = value
        else:
            migrated[str(raw_key)] = value

    if provider:
        migrated["default_provider"] = provider
        if model:
            providers.setdefault(provider, {})["model"] = model
    elif model:
        migrated["model"] = model
    if providers:
        migrated["providers"] = providers
    return migrated


def _flatten_for_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Map the v2 layout onto flat ``VikramSettings`` field names."""
    config: dict[str, Any] = {}
    default_provider = data.get("default_provider")
    if isinstance(default_provider, str) and default_provider:
        # Deliberately not model_provider: the file's default must rank below
        # an agent spec's pinned provider, unlike an explicit env override.
        config["config_default_provider"] = default_provider
    model = data.get("model")
    if isinstance(model, str) and model:
        config["model"] = model

    agents_table = data.get("agents")
    agent_overrides: dict[str, dict[str, str]] = {}
    if isinstance(agents_table, dict):
        for agent_id, section in agents_table.items():
            if not isinstance(section, dict):
                continue
            override = {
                key: value
                for key, value in section.items()
                if key in ("provider", "model") and isinstance(value, str) and value
            }
            if override:
                agent_overrides[str(agent_id)] = override
    if agent_overrides:
        config["agent_overrides"] = agent_overrides

    providers_table = data.get("providers")
    provider_models: dict[str, str] = {}
    if isinstance(providers_table, dict):
        for provider_id, section in providers_table.items():
            if not isinstance(section, dict):
                continue
            section_model = section.get("model")
            if isinstance(section_model, str) and section_model:
                provider_models[str(provider_id)] = section_model
            entry = PROVIDERS.get(str(provider_id))
            if entry is None:
                continue
            api_key = section.get("api_key")
            if entry.api_key_field and isinstance(api_key, str) and api_key:
                config[entry.api_key_field] = api_key
            base_url = section.get("base_url")
            if entry.base_url_field and isinstance(base_url, str) and base_url:
                config[entry.base_url_field] = base_url
    if provider_models:
        config["provider_models"] = provider_models
    return config


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Config as consumed by ``VikramSettings`` (its lowest-priority source)."""
    try:
        raw = load_config_raw(path)
    except ConfigParseError:
        return {}
    return _flatten_for_settings(migrate_v1(raw))


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bare_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return _toml_quote(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return _toml_quote(str(value))


def _ordered_scalar_keys(table: dict[str, Any], path: tuple[str, ...]) -> list[str]:
    keys = [key for key, value in table.items() if not isinstance(value, dict)]
    preferred = _TOP_LEVEL_KEY_ORDER if not path else _PROVIDER_SECTION_KEY_ORDER
    ordered = [key for key in preferred if key in keys]
    ordered.extend(sorted(key for key in keys if key not in preferred))
    return ordered


def _ordered_subtable_keys(table: dict[str, Any], path: tuple[str, ...]) -> list[str]:
    keys = [key for key, value in table.items() if isinstance(value, dict)]
    if not path:
        ordered = [key for key in ("providers", "agents") if key in keys]
        ordered.extend(
            sorted(key for key in keys if key not in ("providers", "agents"))
        )
        return ordered
    if path == ("providers",):
        ordered = [key for key in PROVIDER_IDS if key in keys]
        ordered.extend(sorted(key for key in keys if key not in PROVIDERS))
        return ordered
    return sorted(keys)


def _emit_table(path: tuple[str, ...], table: dict[str, Any], lines: list[str]) -> None:
    scalar_keys = _ordered_scalar_keys(table, path)
    subtable_keys = _ordered_subtable_keys(table, path)
    # Pure parent tables ([providers] with only subtables) need no header;
    # empty tables keep one so hand-added sections survive a round-trip.
    if path and (scalar_keys or not subtable_keys):
        lines.append("[" + ".".join(_bare_key(part) for part in path) + "]")
    for key in scalar_keys:
        lines.append(f"{_bare_key(key)} = {_toml_value(table[key])}")
    if scalar_keys or (path and not subtable_keys):
        lines.append("")
    for key in subtable_keys:
        _emit_table((*path, key), table[key], lines)


def _emit_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _emit_table((), data, lines)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def merge_write_config(
    updates: dict[str, Any],
    *,
    default_provider: str | None = None,
    path: Path | None = None,
) -> Path:
    """Merge ``updates`` into config.toml and write it atomically (0600).

    Existing providers, unknown keys and foreign sections are preserved; a
    corrupt existing file is backed up to ``config.toml.bak`` instead of
    being silently replaced.
    """
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        existing = load_config_raw(path)
    except ConfigParseError as exc:
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
        print(
            f"Warning: {exc}. The unreadable file was backed up to {backup}.",
            file=sys.stderr,
        )
        existing = {}

    merged = _deep_merge(migrate_v1(existing), updates)
    merged["config_version"] = CONFIG_VERSION
    if default_provider:
        merged["default_provider"] = default_provider

    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(_FILE_HEADER)
        file.write(_emit_toml(merged))
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)
    return path


def write_agent_model(
    agent_id: str,
    *,
    provider: str,
    model: str,
    path: Path | None = None,
) -> Path:
    """Persist an agent's model choice to ``[agents.<agent_id>]`` (merged)."""
    return merge_write_config(
        {"agents": {agent_id: {"provider": provider, "model": model}}},
        path=path,
    )


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="vikram configure",
        description=(
            f"Configure model providers in {config_path()}. Safe to re-run: "
            "existing providers are kept and merged."
        ),
        epilog="Also available as: vikram setup",
    )


def _resolve_provider_choice(choice: str) -> Provider | None:
    value = choice.strip().lower()
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(PROVIDER_IDS):
            return PROVIDERS[PROVIDER_IDS[index]]
        return None
    return PROVIDERS.get(value)


def _print_provider_menu(
    providers_state: dict[str, dict[str, Any]],
    output_fn: Callable[[str], None],
) -> None:
    for index, entry in enumerate(PROVIDERS.values(), start=1):
        section = providers_state.get(entry.id) or {}
        model = section.get("model")
        status = f"configured: {model}" if model else "not configured"
        output_fn(f"  {index}) {entry.display_name:<36} [{status}]")


def _prompt_provider_section(
    entry: Provider,
    current: dict[str, Any],
    *,
    input_fn: Callable[[str], str],
    secret_input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> dict[str, Any]:
    section = dict(current)

    model_suggestion = current.get("model") or entry.suggested_model
    while True:
        prompt = f"Model [{model_suggestion}]: " if model_suggestion else "Model: "
        value = input_fn(prompt).strip()
        if value:
            section["model"] = value
            break
        if model_suggestion:
            section["model"] = model_suggestion
            break
        output_fn("Value is required.")

    if entry.prompt_base_url:
        shown_default = current.get("base_url") or entry.default_base_url
        hint = f" ({entry.base_url_hint})" if entry.base_url_hint else ""
        value = input_fn(f"Base URL{hint} [{shown_default}]: ").strip()
        if value:
            section["base_url"] = value

    if entry.needs_api_key:
        has_existing = bool(current.get("api_key"))
        suffix = " [keep existing]" if has_existing else ""
        while True:
            value = secret_input_fn(f"API key ({entry.api_key_env}){suffix}: ").strip()
            if value:
                section["api_key"] = value
                break
            if has_existing:
                break
            output_fn("Value is required.")

    return section


def configure_interactive(
    *,
    input_fn: Callable[[str], str] | None = None,
    secret_input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] = print,
    path: Path | None = None,
) -> Path | None:
    """Multi-provider wizard. Returns the written path, or None if unchanged."""
    input_fn = input_fn or input
    secret_input_fn = secret_input_fn or getpass.getpass

    target = path or config_path()
    try:
        existing = migrate_v1(load_config_raw(target))
    except ConfigParseError:
        existing = {}  # merge_write_config backs up the unreadable file
    providers_state: dict[str, dict[str, Any]] = {
        str(provider_id): dict(section)
        for provider_id, section in (existing.get("providers") or {}).items()
        if isinstance(section, dict)
    }

    output_fn(f"Vikram model configuration — stored in {target}.")
    output_fn("Existing providers are kept; add or update one or more below.")

    updated: dict[str, dict[str, Any]] = {}
    while True:
        output_fn("")
        _print_provider_menu(providers_state, output_fn)
        try:
            choice = input_fn(
                "Provider to configure (number or name, blank to finish): "
            ).strip()
        except EOFError:
            choice = ""
        if not choice:
            break
        entry = _resolve_provider_choice(choice)
        if entry is None:
            output_fn(f"Unknown provider: {choice!r}")
            continue
        section = _prompt_provider_section(
            entry,
            providers_state.get(entry.id) or {},
            input_fn=input_fn,
            secret_input_fn=secret_input_fn,
            output_fn=output_fn,
        )
        providers_state[entry.id] = section
        updated[entry.id] = section
        output_fn(f"{entry.display_name} configured.")

    configured = [
        provider_id
        for provider_id in providers_state
        if providers_state[provider_id].get("model")
    ]
    previous_default = existing.get("default_provider")
    default_provider: str | None = None
    if configured:
        if len(configured) == 1:
            default_provider = configured[0]
            output_fn(f"Default provider: {default_provider}")
        else:
            suggestion = (
                previous_default
                if previous_default in configured
                else next(iter(updated), configured[0])
            )
            while True:
                try:
                    value = input_fn(
                        f"Default provider ({', '.join(configured)}) "
                        f"[{suggestion}]: "
                    ).strip()
                except EOFError:
                    value = ""
                if not value:
                    default_provider = suggestion
                    break
                resolved = _resolve_provider_choice(value)
                if resolved is not None and resolved.id in configured:
                    default_provider = resolved.id
                    break
                output_fn(f"Choose one of: {', '.join(configured)}")

    if not updated and default_provider == previous_default:
        return None

    updates: dict[str, Any] = {"providers": updated} if updated else {}
    return merge_write_config(updates, default_provider=default_provider, path=path)


def run_configure(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    parser.parse_args(argv)

    try:
        path = configure_interactive()
    except (EOFError, KeyboardInterrupt):
        print("\nConfiguration cancelled.", file=sys.stderr)
        return 1

    if path is None:
        print("No changes made.")
        return 0
    print(f"Wrote local model config: {path}")
    return 0
