from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

Status = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class Diagnostic:
    name: str
    status: Status
    detail: str
    fix: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vikram doctor",
        description="Check Vikram's local configuration and workspace setup.",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent spec to validate (default: configured agent).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit diagnostics as JSON.",
    )
    return parser


def collect_diagnostics(
    *,
    agent_name: str | None = None,
    cwd: Path | None = None,
    config_file: Path | None = None,
) -> list[Diagnostic]:
    from vikram.config import config_path
    from vikram.settings import VikramSettings
    from vikram.spec import load_spec

    cwd = (cwd or Path.cwd()).resolve()
    config_file = config_file or config_path()
    diagnostics = [_python_diagnostic(), _config_file_diagnostic(config_file)]

    try:
        settings = VikramSettings()
    except Exception as exc:
        diagnostics.append(
            Diagnostic(
                "Settings",
                "error",
                _single_line(exc),
                "Fix the reported setting or run `vikram configure`.",
            )
        )
        diagnostics.append(_git_diagnostic(cwd))
        return diagnostics

    selected_agent = agent_name or settings.default_agent
    spec = None
    if settings.spec_root.is_dir():
        diagnostics.append(Diagnostic("Spec root", "ok", str(settings.spec_root)))
        try:
            spec = load_spec(selected_agent, settings.spec_root)
        except Exception as exc:
            diagnostics.append(
                Diagnostic(
                    "Agent spec",
                    "error",
                    f"{selected_agent}: {_single_line(exc)}",
                    "Check --agent or VIKRAM_SPEC_ROOT.",
                )
            )
        else:
            diagnostics.append(
                Diagnostic(
                    "Agent spec",
                    "ok",
                    f"{spec.name} ({selected_agent})",
                )
            )
    else:
        diagnostics.append(
            Diagnostic(
                "Spec root",
                "error",
                f"Directory not found: {settings.spec_root}",
                "Set VIKRAM_SPEC_ROOT to a directory containing agent specs.",
            )
        )

    provider = settings.model_provider or (spec.model_provider if spec else None)
    model = settings.model or (spec.model if spec else None)
    diagnostics.append(
        Diagnostic(
            "Model provider",
            "ok" if provider else "error",
            provider or "not configured",
            (
                None
                if provider
                else "Run `vikram configure` or select a configured agent."
            ),
        )
    )
    diagnostics.append(
        Diagnostic(
            "Model",
            "ok" if model else "error",
            model or "not configured",
            None if model else "Run `vikram configure` or set VIKRAM_MODEL.",
        )
    )
    if provider == "openai-compatible":
        has_key = bool(settings.openai_compat_api_key)
        diagnostics.append(
            Diagnostic(
                "API credential",
                "ok" if has_key else "error",
                "available" if has_key else "missing",
                (
                    None
                    if has_key
                    else "Set VIKRAM_OPENAI_COMPAT_API_KEY or run `vikram configure`."
                ),
            )
        )

    if spec is not None and "run_command" in spec.tools:
        try:
            spec.load_command_policy()
        except Exception as exc:
            diagnostics.append(
                Diagnostic(
                    "Command policy",
                    "error",
                    _single_line(exc),
                    "Repair the command policy referenced by the agent spec.",
                )
            )
        else:
            diagnostics.append(
                Diagnostic("Command policy", "ok", str(spec.command_policy))
            )

    diagnostics.append(_git_diagnostic(cwd))
    return diagnostics


def _python_diagnostic() -> Diagnostic:
    version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    supported = sys.version_info >= (3, 13)
    return Diagnostic(
        "Python",
        "ok" if supported else "error",
        version,
        None if supported else "Install Python 3.13 or newer.",
    )


def _config_file_diagnostic(path: Path) -> Diagnostic:
    if not path.is_file():
        return Diagnostic(
            "Config file",
            "warning",
            f"not found: {path}",
            "Run `vikram configure` if settings are not supplied by the environment.",
        )
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return Diagnostic(
            "Config file",
            "error",
            f"invalid TOML: {_single_line(exc)}",
            f"Repair {path} or run `vikram configure`.",
        )
    return Diagnostic("Config file", "ok", str(path))


def _git_diagnostic(cwd: Path) -> Diagnostic:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        return Diagnostic(
            "Git workspace",
            "warning",
            _single_line(exc),
            "Install Git to enable repository-aware coding workflows.",
        )
    if result.returncode:
        return Diagnostic(
            "Git workspace",
            "warning",
            f"not a Git repository: {cwd}",
            "Run Vikram from a repository for safer change review.",
        )
    return Diagnostic("Git workspace", "ok", result.stdout.strip())


def _single_line(value: object) -> str:
    return " ".join(str(value).splitlines())


def _print_table(diagnostics: list[Diagnostic]) -> None:
    from rich.console import Console
    from rich.table import Table

    markers = {
        "ok": "[green]✓[/green]",
        "warning": "[yellow]![/yellow]",
        "error": "[red]✗[/red]",
    }
    table = Table(title="Vikram doctor", box=None, show_header=True)
    table.add_column("", width=1)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Result")
    for diagnostic in diagnostics:
        detail = diagnostic.detail
        if diagnostic.fix:
            detail = f"{detail}\n[dim]{diagnostic.fix}[/dim]"
        table.add_row(markers[diagnostic.status], diagnostic.name, detail)
    Console().print(table)


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    diagnostics = collect_diagnostics(agent_name=args.agent)
    if args.json:
        print(json.dumps({"diagnostics": [asdict(item) for item in diagnostics]}))
    else:
        _print_table(diagnostics)
    return 1 if any(item.status == "error" for item in diagnostics) else 0
