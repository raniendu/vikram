from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Sequence

import pytest

from vikram import update as update_module


def _redirect_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VIKRAM_INSTALL_DIR", raising=False)
    return tmp_path / "vikram" / "install.toml"


def test_metadata_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    meta_file = _redirect_config_dir(monkeypatch, tmp_path)

    update_module.write_metadata(
        {
            "source_dir": "/tmp/some path/vikram",
            "installed_at": "2026-05-25T22:00:00+00:00",
            "python_version": "3.13",
            "git_sha": "abc1234def5678",
        }
    )

    assert meta_file.is_file()
    loaded = update_module.load_metadata()
    assert loaded["source_dir"] == "/tmp/some path/vikram"
    assert loaded["git_sha"] == "abc1234def5678"
    assert loaded["python_version"] == "3.13"


def test_load_metadata_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config_dir(monkeypatch, tmp_path)
    assert update_module.load_metadata() == {}


def test_toml_quote_escapes_special_chars() -> None:
    assert update_module._toml_quote('path with "quote"') == r'"path with \"quote\""'
    assert update_module._toml_quote(r"back\slash") == r'"back\\slash"'


def test_resolve_source_prefers_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config_dir(monkeypatch, tmp_path)
    explicit = tmp_path / "explicit"
    (explicit / ".git").mkdir(parents=True)
    assert update_module.resolve_source(str(explicit)) == explicit


def test_resolve_source_uses_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config_dir(monkeypatch, tmp_path)
    meta_src = tmp_path / "from-meta"
    (meta_src / ".git").mkdir(parents=True)
    update_module.write_metadata({"source_dir": str(meta_src)})

    assert update_module.resolve_source(None) == meta_src


def test_resolve_source_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config_dir(monkeypatch, tmp_path)
    env_src = tmp_path / "from-env"
    (env_src / ".git").mkdir(parents=True)
    monkeypatch.setenv("VIKRAM_INSTALL_DIR", str(env_src))

    assert update_module.resolve_source(None) == env_src


def test_resolve_source_raises_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(update_module, "DEFAULT_INSTALL_DIR", tmp_path / "nope")

    with pytest.raises(update_module.UpdateError):
        update_module.resolve_source(None)


class GitFake:
    """In-memory git double driven by a script of canned responses."""

    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str], *, cwd: Path, capture: bool = True) -> str:
        key = tuple(args)
        self.calls.append(key)
        if key in self.responses:
            return self.responses[key]
        # Mutating commands (fetch, pull, checkout) — return empty by default.
        if args and args[0] in {"fetch", "pull", "checkout"}:
            return ""
        raise AssertionError(f"Unexpected git call: {key}")


def _wire_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake source tree with a pyproject and the .git marker."""
    meta_file = _redirect_config_dir(monkeypatch, tmp_path)
    source = tmp_path / "vikram-src"
    (source / ".git").mkdir(parents=True)
    (source / "vikram").mkdir()
    (source / "pyproject.toml").write_text("# stub\n", encoding="utf-8")
    return source, meta_file


def _stub_path_with_git_and_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    # `shutil.which` is what update.py uses for git/uv presence checks.
    monkeypatch.setattr(
        update_module.shutil,
        "which",
        lambda name: f"/fake/{name}",
    )


def test_run_check_reports_up_to_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _ = _wire_source(monkeypatch, tmp_path)
    _stub_path_with_git_and_uv(monkeypatch)

    sha = "a" * 40
    git = GitFake(
        {
            ("status", "--porcelain"): "",
            ("rev-parse", "HEAD"): sha,
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "origin/main"): sha,
        }
    )
    monkeypatch.setattr(update_module, "_git", git)

    rc = update_module.run(["--check", "--source", str(source)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Already up to date" in out
    assert sha[:12] in out
    assert ("fetch", "--quiet", "origin") in git.calls


def test_run_check_reports_pending_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _ = _wire_source(monkeypatch, tmp_path)
    _stub_path_with_git_and_uv(monkeypatch)

    old = "a" * 40
    new = "b" * 40
    git = GitFake(
        {
            ("status", "--porcelain"): "",
            ("rev-parse", "HEAD"): old,
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "origin/main"): new,
            ("rev-list", "--count", f"{old}..{new}"): "3",
        }
    )
    monkeypatch.setattr(update_module, "_git", git)

    rc = update_module.run(["--check", "--json", "--source", str(source)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "pending",
        "old_sha": old,
        "new_sha": new,
        "commits": 3,
        "source": str(source),
    }


def test_run_aborts_on_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _ = _wire_source(monkeypatch, tmp_path)
    _stub_path_with_git_and_uv(monkeypatch)

    git = GitFake({("status", "--porcelain"): " M vikram/cli.py"})
    monkeypatch.setattr(update_module, "_git", git)

    rc = update_module.run(["--source", str(source)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "uncommitted changes" in err


def test_run_aborts_when_git_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _ = _wire_source(monkeypatch, tmp_path)
    monkeypatch.setattr(
        update_module.shutil, "which", lambda name: None if name == "git" else "/x"
    )

    rc = update_module.run(["--source", str(source)])
    assert rc == 1
    assert "git is not on PATH" in capsys.readouterr().err


def test_run_full_update_writes_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, meta_file = _wire_source(monkeypatch, tmp_path)
    _stub_path_with_git_and_uv(monkeypatch)

    old = "a" * 40
    new = "b" * 40
    head_responses = iter([old, new])
    rev_parse_calls = {"count": 0}

    def fake_git(args: Sequence[str], *, cwd: Path, capture: bool = True) -> str:
        key = tuple(args)
        if key == ("status", "--porcelain"):
            return ""
        if key == ("rev-parse", "HEAD"):
            rev_parse_calls["count"] += 1
            return next(head_responses)
        if key == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if key == ("rev-parse", "origin/main"):
            return new
        if key == ("rev-list", "--count", f"{old}..{new}"):
            return "2"
        if args and args[0] in {"fetch", "pull"}:
            return ""
        raise AssertionError(f"Unexpected git call: {key}")

    monkeypatch.setattr(update_module, "_git", fake_git)

    install_calls: list[Sequence[str]] = []

    def fake_run(
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        install_calls.append(cmd)
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(update_module, "_run", fake_run)

    rc = update_module.run(["--source", str(source)])

    assert rc == 0
    out = capsys.readouterr().out
    assert old[:12] in out and new[:12] in out
    assert "2 commit" in out

    assert len(install_calls) == 1
    cmd = install_calls[0]
    assert cmd[0:4] == ["uv", "tool", "install", "--force"]
    # --reinstall-package vikram is required so uv rebuilds the wheel from the
    # local source instead of reusing a cached wheel for vikram==0.1.0.
    assert "--reinstall-package" in cmd
    pkg_idx = cmd.index("--reinstall-package")
    assert cmd[pkg_idx + 1] == "vikram"
    assert "--from" in cmd
    assert str(source) in cmd
    assert "vikram" == cmd[-1]

    assert meta_file.is_file()
    saved = update_module.load_metadata()
    assert saved["source_dir"] == str(source)
    assert saved["git_sha"] == new


def test_run_reinstalls_when_installed_sha_lags_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _ = _wire_source(monkeypatch, tmp_path)
    _stub_path_with_git_and_uv(monkeypatch)

    installed = "a" * 40
    current = "b" * 40
    update_module.write_metadata(
        {
            "source_dir": str(source),
            "python_version": "3.13",
            "git_sha": installed,
        }
    )

    git = GitFake(
        {
            ("status", "--porcelain"): "",
            ("rev-parse", "HEAD"): current,
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "origin/main"): current,
        }
    )
    monkeypatch.setattr(update_module, "_git", git)

    install_calls: list[Sequence[str]] = []

    def fake_run(
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        install_calls.append(cmd)
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(update_module, "_run", fake_run)

    rc = update_module.run(["--source", str(source)])

    assert rc == 0
    out = capsys.readouterr().out
    assert installed[:12] in out and current[:12] in out
    assert "reinstalled" in out
    assert ("pull", "--quiet", "--ff-only") not in git.calls

    assert len(install_calls) == 1
    cmd = install_calls[0]
    assert "--reinstall-package" in cmd
    assert "--from" in cmd
    assert str(source) in cmd

    saved = update_module.load_metadata()
    assert saved["git_sha"] == current


def test_spec_root_falls_back_to_install_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When package-relative spec/ is missing (the installed-as-tool layout),
    the resolver should return ``<metadata.source_dir>/spec``."""
    _redirect_config_dir(monkeypatch, tmp_path)
    fake_source = tmp_path / "vikram-src"
    spec_dir = fake_source / "spec"
    spec_dir.mkdir(parents=True)
    update_module.write_metadata({"source_dir": str(fake_source)})

    from vikram.settings import _resolve_spec_root

    missing_pkg_relative = tmp_path / "site-packages" / "spec"
    assert _resolve_spec_root(missing_pkg_relative) == spec_dir


def test_spec_root_prefers_package_relative(tmp_path: Path) -> None:
    """If the package-relative spec/ exists (dev layout), use it as-is."""
    pkg_relative = tmp_path / "spec"
    pkg_relative.mkdir()

    from vikram.settings import _resolve_spec_root

    assert _resolve_spec_root(pkg_relative) == pkg_relative


def test_spec_root_returns_package_relative_when_no_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No metadata file and a missing package-relative path: return the
    package-relative path so the eventual FileNotFoundError points somewhere
    predictable."""
    _redirect_config_dir(monkeypatch, tmp_path)

    from vikram.settings import _resolve_spec_root

    missing = tmp_path / "nope" / "spec"
    assert _resolve_spec_root(missing) == missing


def test_run_full_update_propagates_uv_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _ = _wire_source(monkeypatch, tmp_path)
    _stub_path_with_git_and_uv(monkeypatch)

    old = "a" * 40
    new = "b" * 40
    head_responses = iter([old, new])

    def fake_git(args: Sequence[str], *, cwd: Path, capture: bool = True) -> str:
        key = tuple(args)
        if key == ("status", "--porcelain"):
            return ""
        if key == ("rev-parse", "HEAD"):
            return next(head_responses)
        if key == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if key == ("rev-parse", "origin/main"):
            return new
        if args and args[0] in {"fetch", "pull"}:
            return ""
        raise AssertionError(f"Unexpected git call: {key}")

    monkeypatch.setattr(update_module, "_git", fake_git)
    monkeypatch.setattr(
        update_module,
        "_run",
        lambda cmd, **_: subprocess.CompletedProcess(
            args=list(cmd), returncode=7, stdout="", stderr=""
        ),
    )

    rc = update_module.run(["--source", str(source)])
    assert rc == 7
    assert "uv tool install failed" in capsys.readouterr().err
