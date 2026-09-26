import json
import shlex
from types import SimpleNamespace

import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import ApprovalRequired

from tests.conftest import find_log_event
from vikram import tools


def _ctx(*, approved: bool = False):
    return SimpleNamespace(tool_call_approved=approved)


@pytest.fixture(autouse=True)
def _reset_command_policy():
    """Keep the module-level command policy from leaking between tests."""
    tools._ACTIVE_POLICY = None
    yield
    tools._ACTIVE_POLICY = None


@pytest.mark.asyncio
async def test_run_command_non_allowlisted_reaches_approval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    decision, _ = tools._policy().classify(shlex.split("echo hello"), "echo hello")
    assert decision == "approve"

    with pytest.raises(ApprovalRequired):
        await tools.run_command(_ctx(), "echo hello")

    ran = await tools.run_command(_ctx(approved=True), "echo hello")
    assert "$ echo hello" in ran

    # Read-only commands still auto-run with no approval.
    allowed = await tools.run_command(_ctx(), "git status --short")
    assert "$ git status --short" in allowed


@pytest.mark.asyncio
async def test_inspect_command_allows_read_only_git_commands(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    branch = await tools.inspect_command("git branch -a")
    remote = await tools.inspect_command("git remote -v")
    current = await tools.inspect_command("git rev-parse --abbrev-ref HEAD")

    assert "$ git branch -a" in branch
    assert "$ git remote -v" in remote
    assert "$ git rev-parse --abbrev-ref HEAD" in current


@pytest.mark.asyncio
async def test_inspect_command_refuses_non_read_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    switch = await tools.inspect_command("git switch main")
    delete_branch = await tools.inspect_command("git branch -D main")
    remote_add = await tools.inspect_command("git remote add origin example")
    diff_output = await tools.inspect_command("git diff --output=patch.txt")

    for refused in (switch, delete_branch, remote_add, diff_output):
        assert "use run_command" in refused


@pytest.mark.asyncio
async def test_run_command_state_changes_reach_approval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # Pydantic AI gates state-changing commands dynamically in the tool body.
    for command in (
        "git switch main",
        "git pull --ff-only",
        "git pull --rebase",
        "git add -A",
        'git commit -m "Update system prompt"',
        "git push -u origin feature-branch",
        'gh pr create --title "Update" --body "body" --base main',
    ):
        decision, reason = tools._policy().classify(shlex.split(command), command)
        assert decision == "approve"
        assert reason is None
        with pytest.raises(ApprovalRequired):
            await tools.run_command(_ctx(), command)

    switch = await tools.run_command(_ctx(approved=True), "git switch main")
    assert "$ git switch main" in switch


@pytest.mark.asyncio
async def test_deny_backstop_refuses_even_when_approved(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    dangerous = (
        ('git commit --no-verify -m "skip hooks"', "no-verify"),
        ('git commit --amend -m "rewrite"', "amending"),
        ("git push --force origin main", "force"),
        ("git push origin :main", "refspec"),
        ("git reset --hard", "hard reset"),
        ("git rebase main", "rebase"),
        ("rm -rf build", "recursive rm"),
        ("sudo rm file", "privilege escalation"),
        ("cat .env.production", "secret"),
    )
    for command, needle in dangerous:
        # Approval does not override the deny backstop.
        result = await tools.run_command(_ctx(approved=True), command)
        assert "Refusing" in result, command
        assert needle in result, command


@pytest.mark.asyncio
async def test_secret_path_deny_excludes_example(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    denied = await tools.run_command(_ctx(approved=True), "cat .env.production")
    assert "Refusing" in denied

    # .env.example is excluded from the secret-path deny, so it reaches HITL.
    decision, _ = tools._policy().classify(
        shlex.split("cat .env.example"), "cat .env.example"
    )
    assert decision == "approve"


@pytest.mark.asyncio
async def test_inspect_command_refuses_deny_and_state_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    commit = await tools.inspect_command('git commit -m "msg"')
    push = await tools.inspect_command("git push")
    force = await tools.inspect_command("git push --force origin main")

    assert "use run_command" in commit
    assert "use run_command" in push
    assert "Refusing" in force  # deny is reported even by inspect_command


@pytest.mark.asyncio
async def test_run_command_argv_only_no_shell(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # A shell metacharacter is just an argv token; no shell expansion happens.
    # `true` is read-only (auto), so this runs without approval and the pipe is
    # passed verbatim as an argument rather than chaining commands.
    result = await tools.run_command(_ctx(), "true | rm -rf /")
    assert "$ true | rm -rf /" in result


def test_destructive_tools_require_pydantic_ai_approval():
    for name in ("write_file", "edit_file"):
        tool = tools.TOOL_REGISTRY[name]

        assert tool.requires_approval is True
        assert tool.sequential is True
        assert tool.name == name


def test_run_command_uses_dynamic_approval():
    tool = tools.TOOL_REGISTRY["run_command"]

    assert isinstance(tool, Tool)
    assert tool.requires_approval is False
    assert tool.sequential is True


def test_read_only_tools_do_not_require_approval():
    for name in ("read_file", "glob", "grep", "inspect_command"):
        assert not getattr(tools.TOOL_REGISTRY[name], "requires_approval", False)


@pytest.mark.asyncio
async def test_web_search_no_api_key(monkeypatch):
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    # Clear the lru_cache for _parallel_client to ensure it checks settings again
    tools._parallel_client.cache_clear()

    result = await tools.web_search("test query")
    assert "PARALLEL_API_KEY is not set" in result


@pytest.mark.asyncio
async def test_denied_command_is_logged_as_a_policy_refusal(
    monkeypatch, tmp_path, log_events
):
    monkeypatch.chdir(tmp_path)

    await tools.run_command(_ctx(approved=True), "git push --force origin main")

    refusal = find_log_event(log_events, "tool_call_refused")
    assert refusal["tool"] == "run_command"
    assert refusal["reason"] == "policy_denied"


@pytest.mark.asyncio
async def test_command_execution_is_logged_without_the_command_string(
    monkeypatch, tmp_path, log_events
):
    monkeypatch.chdir(tmp_path)

    # `true` is classified read-only, so it runs without approval. The token
    # here stands in for a credential passed as a command-line flag.
    await tools.run_command(_ctx(approved=True), "true --token supersecretvalue")

    started = find_log_event(log_events, "tool_command_started")
    assert started["executable"] == "true"
    assert started["argument_count"] == 2

    finished = find_log_event(log_events, "tool_command_finished")
    assert finished["exit_code"] == 0
    assert finished["duration_ms"] >= 0

    # No log line may carry the argument values themselves.
    assert "supersecretvalue" not in json.dumps(log_events, default=str)


@pytest.mark.asyncio
async def test_missing_command_is_logged_not_silently_swallowed(
    monkeypatch, tmp_path, log_events
):
    monkeypatch.chdir(tmp_path)

    result = await tools.run_command(
        _ctx(approved=True), "definitely-not-a-real-binary-xyz"
    )

    assert "Command not found" in result
    not_found = find_log_event(log_events, "tool_command_not_found")
    assert not_found["log_level"] == "warning"
    assert not_found["executable"] == "definitely-not-a-real-binary-xyz"
