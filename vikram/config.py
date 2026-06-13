from __future__ import annotations

import argparse
import getpass
import os
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

ModelProvider = Literal["ollama", "openai-compatible"]

CONFIG_FILE_NAME = "config.toml"

CONFIG_KEYS = {
    "model_provider",
    "model",
    "ollama_base_url",
    "openai_compat_api_key",
    "openai_compat_base_url",
}

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


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "vikram"


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def _normalize_key(key: str) -> str:
    return ENV_KEY_MAP.get(key.upper(), key)


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if not path.is_file():
        return {}

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    config: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = _normalize_key(str(raw_key))
        if key in CONFIG_KEYS and value not in (None, ""):
            config[key] = value
    return config


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_model_config(
    *,
    model_provider: ModelProvider,
    model: str,
    ollama_base_url: str | None = None,
    openai_compat_api_key: str | None = None,
    openai_compat_base_url: str | None = None,
    path: Path | None = None,
) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    lines = ["# Written by `vikram configure`.\n"]
    lines.append(f"model_provider = {_toml_quote(model_provider)}\n")
    lines.append(f"model = {_toml_quote(model)}\n")
    if model_provider == "ollama" and ollama_base_url:
        lines.append(f"ollama_base_url = {_toml_quote(ollama_base_url)}\n")
    if model_provider == "openai-compatible":
        if openai_compat_api_key:
            lines.append(
                f"openai_compat_api_key = {_toml_quote(openai_compat_api_key)}\n"
            )
        if openai_compat_base_url:
            lines.append(
                f"openai_compat_base_url = {_toml_quote(openai_compat_base_url)}\n"
            )

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write("".join(lines))
    os.chmod(path, 0o600)
    return path


def _prompt_required(
    prompt: str,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    while True:
        value = input_fn(prompt).strip()
        if value:
            return value
        output_fn("Value is required.")


def _prompt_provider(
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> ModelProvider:
    while True:
        value = input_fn("Model provider (ollama/openai-compatible): ").strip()
        if value in {"ollama", "openai-compatible"}:
            return value  # type: ignore[return-value]
        output_fn("Choose exactly: ollama or openai-compatible.")


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="vikram configure",
        description=f"Write local model config to {config_path()}.",
    )


def configure_interactive(
    *,
    input_fn: Callable[[str], str] | None = None,
    secret_input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] = print,
    path: Path | None = None,
) -> Path:
    input_fn = input_fn or input
    secret_input_fn = secret_input_fn or getpass.getpass

    output_fn("Vikram has no default model. Configure the model you want to use.")
    provider = _prompt_provider(input_fn=input_fn, output_fn=output_fn)
    model = _prompt_required("Model name: ", input_fn=input_fn, output_fn=output_fn)

    if provider == "ollama":
        ollama_base_url = input_fn(
            "Ollama base URL (blank uses http://localhost:11434/v1): "
        ).strip()
        return write_model_config(
            model_provider=provider,
            model=model,
            ollama_base_url=ollama_base_url or None,
            path=path,
        )

    base_url = input_fn(
        "OpenAI-compatible base URL (blank uses https://api.openai.com/v1,\n"
        "  DigitalOcean serverless inference: https://inference.do-ai.run/v1\n"
        "  Sarvam AI: https://api.sarvam.ai/v1): "
    ).strip()
    api_key = _prompt_required(
        "API key (DIGITALOCEAN_ACCESS_TOKEN / SARVAM_API_KEY also accepted): ",
        input_fn=secret_input_fn,
        output_fn=output_fn,
    )
    return write_model_config(
        model_provider=provider,
        model=model,
        openai_compat_api_key=api_key,
        openai_compat_base_url=base_url or None,
        path=path,
    )


def run_configure(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    parser.parse_args(argv)

    try:
        path = configure_interactive()
    except (EOFError, KeyboardInterrupt):
        print("\nConfiguration cancelled.", file=sys.stderr)
        return 1

    print(f"Wrote local model config: {path}")
    return 0
