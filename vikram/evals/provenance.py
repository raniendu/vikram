"""Identify the agent under evaluation without saving source or prompt content."""

import hashlib
import platform
import subprocess
from importlib.metadata import distributions, version
from pathlib import Path

from vikram.evals.history import AgentRevision, fingerprint


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def capture_revision() -> AgentRevision:
    """Capture runtime code, dependencies, and Git identity, including local edits."""
    package = Path(__file__).resolve().parents[1]
    files = {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*.py"))
        if "evals" not in path.relative_to(package).parts
        and "__pycache__" not in path.parts
    }
    root_name = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    root = Path(root_name) if root_name else Path.cwd()
    dependencies = {
        distribution.metadata["Name"]: distribution.version
        for distribution in distributions()
        if distribution.metadata["Name"]
    }
    manifests = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("pyproject.toml", "uv.lock")
        if (root / name).is_file()
    }
    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    return AgentRevision(
        source_hash=fingerprint(files),
        dependencies_hash=fingerprint([dependencies, manifests]),
        commit=_git(root, "rev-parse", "HEAD"),
        dirty=None if status is None else bool(status),
        harness={
            "python": platform.python_version(),
            "pydantic-ai": version("pydantic-ai-slim"),
            "pydantic-evals": version("pydantic-evals"),
        },
    )
