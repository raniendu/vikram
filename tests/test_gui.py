from __future__ import annotations

import pytest

from vikram import gui


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(gui.API_BIN_ENV, raising=False)
    monkeypatch.delenv(gui.APP_ENV, raising=False)


# --- finding vikram-api -------------------------------------------------
#
# This is the whole reason `vikram gui` exists: a Finder-launched .app
# inherits no login PATH and cannot see ~/.local/bin/vikram-api, so the
# launcher resolves an absolute path and passes it through.


def test_explicit_env_wins(monkeypatch, tmp_path):
    binary = tmp_path / "vikram-api"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv(gui.API_BIN_ENV, str(binary))

    assert gui.find_api_binary() == binary


def test_ignores_an_env_path_that_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv(gui.API_BIN_ENV, str(tmp_path / "missing"))
    monkeypatch.setattr(gui.shutil, "which", lambda _: None)
    monkeypatch.setattr(gui.Path, "home", staticmethod(lambda: tmp_path))

    assert gui.find_api_binary() is None


def test_falls_back_to_path(monkeypatch, tmp_path):
    binary = tmp_path / "vikram-api"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(gui.shutil, "which", lambda _: str(binary))

    assert gui.find_api_binary() == binary.resolve()


def test_probes_local_bin_when_path_is_empty(monkeypatch, tmp_path):
    """The Finder case: no PATH, but uv put the binary in ~/.local/bin."""
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "vikram-api").write_text("#!/bin/sh\n")
    monkeypatch.setattr(gui.shutil, "which", lambda _: None)
    monkeypatch.setattr(gui.Path, "home", staticmethod(lambda: tmp_path))

    assert gui.find_api_binary() == local_bin / "vikram-api"


def test_run_explains_itself_when_the_api_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(gui, "find_api_binary", lambda: None)

    assert gui.run([]) == 1
    assert "uv tool install" in capsys.readouterr().err


# --- launching ----------------------------------------------------------


def test_run_passes_the_resolved_path_to_the_app(monkeypatch, tmp_path):
    binary = tmp_path / "vikram-api"
    binary.write_text("#!/bin/sh\n")
    bundle = tmp_path / "Vikram Studio.app"
    executable = bundle / "Contents" / "MacOS" / "vikram-studio"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    monkeypatch.setattr(gui, "find_api_binary", lambda: binary)
    monkeypatch.setattr(gui, "find_bundle", lambda: bundle)
    launched: dict = {}
    monkeypatch.setenv("VIKRAM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        gui.subprocess,
        "Popen",
        lambda cmd, **kw: launched.update(cmd=cmd, env=kw.get("env")),
    )

    assert gui.run([]) == 0
    assert launched["cmd"] == [str(executable)]
    assert launched["env"][gui.API_BIN_ENV] == str(binary)


def test_run_reports_a_missing_bundle_with_build_instructions(
    monkeypatch, tmp_path, capsys
):
    binary = tmp_path / "vikram-api"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(gui, "find_api_binary", lambda: binary)
    monkeypatch.setattr(gui, "find_bundle", lambda: None)

    assert gui.run([]) == 1
    err = capsys.readouterr().err
    assert "vikram gui --build" in err
    assert "--dev" in err


def test_dev_mode_runs_tauri_from_the_checkout(monkeypatch, tmp_path):
    binary = tmp_path / "vikram-api"
    binary.write_text("#!/bin/sh\n")
    gui_dir = tmp_path / "gui"
    (gui_dir / "node_modules").mkdir(parents=True)
    (gui_dir / "package.json").write_text("{}")

    monkeypatch.setattr(gui, "find_api_binary", lambda: binary)
    monkeypatch.setattr(gui, "repo_gui_dir", lambda: gui_dir)
    calls: list = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda cmd, **kw: calls.append((cmd, kw)) or _Done(),
    )

    assert gui.run(["--dev"]) == 0
    assert calls[0][0] == ["npm", "run", "tauri", "dev"]
    assert calls[0][1]["cwd"] == gui_dir
    assert calls[0][1]["env"][gui.API_BIN_ENV] == str(binary)


def test_bundle_executable_is_launched_directly_not_via_open(tmp_path):
    """`open -a` cannot reliably pass environment, which is the point here."""
    bundle = tmp_path / "App.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "app-binary"
    binary.write_text("")
    binary.chmod(0o755)

    assert gui._bundle_executable(bundle) == binary


def test_bundle_without_an_executable_is_none(tmp_path):
    bundle = tmp_path / "App.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)

    assert gui._bundle_executable(bundle) is None


# --- CLI dispatch -------------------------------------------------------


def test_cli_dispatches_the_gui_subcommand(monkeypatch):
    from vikram import cli

    seen: dict = {}

    def fake_run(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(gui, "run", fake_run)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["gui", "--dev"])

    assert exit_info.value.code == 0
    assert seen["argv"] == ["--dev"]


def test_gui_is_listed_as_a_command():
    from vikram.cli import COMMANDS

    assert "gui" in COMMANDS


def test_launch_detaches_so_the_shell_returns(monkeypatch, tmp_path):
    """An attached child spams the terminal and blocks anything piping us."""
    binary = tmp_path / "vikram-api"
    binary.write_text("#!/bin/sh\n")
    bundle = tmp_path / "Vikram Studio.app"
    executable = bundle / "Contents" / "MacOS" / "vikram-studio"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    monkeypatch.setattr(gui, "find_api_binary", lambda: binary)
    monkeypatch.setattr(gui, "find_bundle", lambda: bundle)
    monkeypatch.setenv("VIKRAM_STATE_DIR", str(tmp_path / "state"))
    seen: dict = {}
    monkeypatch.setattr(gui.subprocess, "Popen", lambda cmd, **kw: seen.update(kw))

    assert gui.run([]) == 0
    assert seen["start_new_session"] is True
    assert seen["stdin"] is gui.subprocess.DEVNULL
    assert seen["stdout"] is seen["stderr"]  # both into the log file
    assert (tmp_path / "state" / "studio.log").exists()


def test_finds_a_bundle_built_in_the_checkout(monkeypatch, tmp_path):
    """Having just run `npm run tauri build` is the normal case."""
    gui_dir = tmp_path / "gui"
    built = (
        gui_dir
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "macos"
        / "Vikram Studio.app"
    )
    built.mkdir(parents=True)
    monkeypatch.setattr(gui, "repo_gui_dir", lambda: gui_dir)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(gui, "BUNDLE_CANDIDATES", ("~/Applications/Nope.app",))

    assert gui.find_bundle() == built


def test_an_installed_bundle_wins_over_the_checkout_build(monkeypatch, tmp_path):
    home = tmp_path / "home"
    installed = home / "Applications" / "Vikram Studio.app"
    installed.mkdir(parents=True)
    gui_dir = tmp_path / "gui"
    (
        gui_dir
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "macos"
        / "Vikram Studio.app"
    ).mkdir(parents=True)
    monkeypatch.setattr(gui, "repo_gui_dir", lambda: gui_dir)
    monkeypatch.setenv("HOME", str(home))

    assert gui.find_bundle() == installed


# --- building -----------------------------------------------------------
#
# `find_bundle` prefers ~/Applications over the checkout's build output, so a
# build that stops at the checkout leaves the stale app winning the lookup.


def test_build_refuses_without_the_toolchain(monkeypatch, capsys):
    monkeypatch.setattr(gui.shutil, "which", lambda _: None)

    assert gui.build_bundle() == 1
    err = capsys.readouterr().err
    assert "npm" in err and "cargo" in err


def test_build_names_only_the_tool_that_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(
        gui.shutil, "which", lambda name: None if name == "cargo" else "/x"
    )

    assert gui.build_bundle() == 1
    err = capsys.readouterr().err
    assert "cargo" in err and "npm," not in err


def test_build_installs_over_a_stale_bundle(monkeypatch, tmp_path):
    gui_dir = tmp_path / "gui"
    gui_dir.mkdir()
    (gui_dir / "package.json").write_text("{}")
    built = gui_dir / "out" / "Vikram Studio.app"
    (built / "Contents").mkdir(parents=True)
    (built / "Contents" / "fresh").write_text("new")

    installed = tmp_path / "Applications"
    stale = installed / "Vikram Studio.app"
    stale.mkdir(parents=True)
    (stale / "stale-marker").write_text("old")

    monkeypatch.setattr(gui.shutil, "which", lambda _: "/usr/bin/x")
    monkeypatch.setattr(gui, "repo_gui_dir", lambda: gui_dir)
    monkeypatch.setattr(gui, "bundle_build_output", lambda: built)
    monkeypatch.setattr(gui, "INSTALL_DIR", installed)

    class _Done:
        returncode = 0

    monkeypatch.setattr(gui.subprocess, "run", lambda cmd, **kw: _Done())

    assert gui.build_bundle() == 0
    assert (stale / "Contents" / "fresh").read_text() == "new"
    assert not (stale / "stale-marker").exists()


def test_build_stops_when_npm_install_fails(monkeypatch, tmp_path):
    gui_dir = tmp_path / "gui"
    gui_dir.mkdir()
    (gui_dir / "package.json").write_text("{}")
    monkeypatch.setattr(gui.shutil, "which", lambda _: "/usr/bin/x")
    monkeypatch.setattr(gui, "repo_gui_dir", lambda: gui_dir)
    calls: list = []

    class _Failed:
        returncode = 3

    monkeypatch.setattr(
        gui.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Failed()
    )

    assert gui.build_bundle() == 3
    assert calls == [["npm", "install"]]  # never reached `tauri build`


def test_build_flag_does_not_require_the_api(monkeypatch):
    """Building is a source operation; a missing vikram-api is irrelevant."""
    monkeypatch.setattr(gui, "find_api_binary", lambda: None)
    monkeypatch.setattr(gui, "build_bundle", lambda: 0)

    assert gui.run(["--build"]) == 0
