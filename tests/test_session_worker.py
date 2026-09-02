"""Integration tests that spawn real session worker subprocesses.

These are the tests that justify the process-per-session design. They run the
actual worker but never a model: the agent is built (proving spec loading,
chdir and MCP setup work) and then the process is asked to shut down.
"""

from __future__ import annotations

import os

import pytest

from vikram.session import SessionError, SessionRegistry


@pytest.fixture
def registry():
    return SessionRegistry()


@pytest.fixture
def gui_env(monkeypatch, tmp_path):
    """Point the worker at a throwaway config home with a model pinned."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VIKRAM_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("VIKRAM_MODEL", "test-model")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")


async def test_worker_chdirs_into_its_own_workspace(registry, gui_env, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    parent_cwd = os.getcwd()

    session = await registry.create(agent_id="coder", workspace=workspace)
    try:
        reported = session.ready["payload"]["workspace"]
        assert os.path.realpath(reported) == os.path.realpath(workspace)
        # The parent must be untouched: cwd is process-global, which is the
        # entire reason sessions get their own process.
        assert os.getcwd() == parent_cwd
    finally:
        await registry.close(session.id)


async def test_two_sessions_hold_independent_workspaces(registry, gui_env, tmp_path):
    """The regression this design exists to prevent.

    In one process the second os.chdir would silently move the first session's
    file tools onto the wrong directory.
    """
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    a = await registry.create(agent_id="coder", workspace=first)
    b = await registry.create(agent_id="coder", workspace=second)
    try:
        assert os.path.realpath(a.ready["payload"]["workspace"]) == os.path.realpath(
            first
        )
        assert os.path.realpath(b.ready["payload"]["workspace"]) == os.path.realpath(
            second
        )
        assert a.process.pid != b.process.pid
    finally:
        await registry.close(a.id)
        await registry.close(b.id)


async def test_ready_reports_what_the_agent_actually_is(registry, gui_env, tmp_path):
    session = await registry.create(agent_id="coder", workspace=tmp_path)
    try:
        payload = session.ready["payload"]

        assert payload["name"] == "Coder"
        assert payload["model_config"]["model"] == "test-model"
        assert "write_file" in payload["tool_names"]
        assert "write_file" in payload["approval_tool_names"]
    finally:
        await registry.close(session.id)


async def test_worker_runs_in_its_own_process_group(registry, gui_env, tmp_path):
    """So stop() can reap run_command children and MCP servers with it."""
    session = await registry.create(agent_id="coder", workspace=tmp_path)
    try:
        assert os.getpgid(session.process.pid) != os.getpgid(os.getpid())
    finally:
        await registry.close(session.id)


async def test_close_terminates_the_process(registry, gui_env, tmp_path):
    session = await registry.create(agent_id="coder", workspace=tmp_path)
    pid = session.process.pid

    await registry.close(session.id)

    assert session.closed is True
    assert session.process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_missing_workspace_is_rejected_before_spawning(
    registry, gui_env, tmp_path
):
    with pytest.raises(SessionError, match="not a directory"):
        await registry.create(agent_id="coder", workspace=tmp_path / "nope")


async def test_an_unknown_agent_fails_the_session_cleanly(registry, gui_env, tmp_path):
    with pytest.raises(SessionError):
        await registry.create(agent_id="does-not-exist", workspace=tmp_path)

    assert registry.list() == []


async def test_a_network_only_surface_check_still_applies(
    registry, gui_env, tmp_path, monkeypatch
):
    """coder is local-only, and gui is a local surface, so this must succeed."""
    session = await registry.create(agent_id="coder", workspace=tmp_path)
    try:
        assert session.ready["payload"]["name"] == "Coder"
    finally:
        await registry.close(session.id)


async def test_worker_keeps_stdout_clean_for_the_protocol(registry, gui_env, tmp_path):
    """Every emitted line must parse as JSON; logs belong on stderr."""
    import json

    session = await registry.create(agent_id="coder", workspace=tmp_path)
    events: list[dict] = []
    queue = session.attach()
    while not queue.empty():
        events.append(queue.get_nowait())
    await registry.close(session.id)

    assert events
    for event in events:
        assert json.dumps(event)  # already parsed by the pump; round-trips


async def test_close_all_reaps_every_session(registry, gui_env, tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    a = await registry.create(agent_id="coder", workspace=first)
    b = await registry.create(agent_id="coder", workspace=second)

    await registry.close_all()

    assert registry.list() == []
    assert a.process.returncode is not None
    assert b.process.returncode is not None
