from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import ApprovalRequired

from vikram import tools


def _ctx(approved: bool = False) -> SimpleNamespace:
    """Minimal RunContext stand-in: command tools only read tool_call_approved."""
    return SimpleNamespace(tool_call_approved=approved)


@pytest.fixture(autouse=True)
def _reset_command_policy():
    """Keep the module-level command policy from leaking between tests."""
    tools._ACTIVE_POLICY = None
    yield
    tools._ACTIVE_POLICY = None


@pytest.mark.asyncio
async def test_read_file_returns_numbered_excerpt(monkeypatch, tmp_path):
    source = tmp_path / "pkg" / "example.py"
    source.parent.mkdir()
    source.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await tools.read_file("pkg/example.py", start_line=2, max_lines=2)

    assert "pkg/example.py:2-3" in result
    assert "2 | beta" in result
    assert "3 | gamma" in result
    assert "1 | alpha" not in result


@pytest.mark.asyncio
async def test_file_tools_refuse_sensitive_paths(monkeypatch, tmp_path):
    secret = tmp_path / ".env.local"
    secret.write_text("TOKEN=secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await tools.read_file(".env.local")

    assert "Refusing" in result
    assert "TOKEN" not in result
    assert "secret" not in result


@pytest.mark.asyncio
async def test_file_tools_refuse_paths_outside_cwd(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    monkeypatch.chdir(workspace)

    result = await tools.read_file(str(outside))

    assert "escapes the workspace" in result
    assert "outside" not in result


@pytest.mark.asyncio
async def test_glob_and_grep_are_cwd_scoped(monkeypatch, tmp_path):
    source = tmp_path / "vikram" / "agent.py"
    source.parent.mkdir()
    source.write_text("def build_agent():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("build_agent secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    glob_result = await tools.glob_paths("**/*.py")
    grep_result = await tools.grep("build_agent")

    assert "vikram/agent.py" in glob_result
    assert ".env.local" not in glob_result
    assert "vikram/agent.py:1:def build_agent():" in grep_result
    assert "secret" not in grep_result


@pytest.mark.asyncio
async def test_write_and_edit_file_operate_relative_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    write_result = await tools.write_file("notes/todo.txt", "hello\n")
    edit_result = await tools.edit_file("notes/todo.txt", "hello", "goodbye")

    assert "Wrote notes/todo.txt" in write_result
    assert "Updated notes/todo.txt" in edit_result
    assert (tmp_path / "notes" / "todo.txt").read_text(encoding="utf-8") == (
        "goodbye\n"
    )


@pytest.mark.asyncio
async def test_edit_file_requires_unique_match(monkeypatch, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await tools.edit_file("notes.txt", "same", "other")

    assert "matched 2 times" in result
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


@pytest.mark.asyncio
async def test_run_command_non_allowlisted_reaches_approval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # Previously "not allowlisted"; now an arbitrary command requires approval.
    with pytest.raises(ApprovalRequired):
        await tools.run_command(_ctx(approved=False), "echo hello")

    approved = await tools.run_command(_ctx(approved=True), "echo hello")
    assert "$ echo hello" in approved

    # Read-only commands still auto-run with no approval.
    allowed = await tools.run_command(_ctx(approved=False), "git status --short")
    assert "$ git status --short" in allowed


@pytest.mark.asyncio
async def test_inspect_command_allows_read_only_git_commands(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    branch = await tools.inspect_command(_ctx(), "git branch -a")
    remote = await tools.inspect_command(_ctx(), "git remote -v")
    current = await tools.inspect_command(_ctx(), "git rev-parse --abbrev-ref HEAD")

    assert "$ git branch -a" in branch
    assert "$ git remote -v" in remote
    assert "$ git rev-parse --abbrev-ref HEAD" in current


@pytest.mark.asyncio
async def test_inspect_command_refuses_non_read_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    switch = await tools.inspect_command(_ctx(), "git switch main")
    delete_branch = await tools.inspect_command(_ctx(), "git branch -D main")
    remote_add = await tools.inspect_command(_ctx(), "git remote add origin example")
    diff_output = await tools.inspect_command(_ctx(), "git diff --output=patch.txt")

    for refused in (switch, delete_branch, remote_add, diff_output):
        assert "use run_command" in refused


@pytest.mark.asyncio
async def test_run_command_state_changes_reach_approval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # All of these now route to approval rather than a hardcoded allowlist.
    for command in (
        "git switch main",
        "git pull --ff-only",
        "git pull --rebase",
        "git add -A",
        'git commit -m "Update system prompt"',
        "git push -u origin feature-branch",
        'gh pr create --title "Update" --body "body" --base main',
    ):
        with pytest.raises(ApprovalRequired):
            await tools.run_command(_ctx(approved=False), command)

    # Once approved, the command actually runs (echoed back).
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

    # .env.example is excluded from the secret-path deny, so it reaches approval.
    with pytest.raises(ApprovalRequired):
        await tools.run_command(_ctx(approved=False), "cat .env.example")


@pytest.mark.asyncio
async def test_inspect_command_refuses_deny_and_state_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    commit = await tools.inspect_command(_ctx(), 'git commit -m "msg"')
    push = await tools.inspect_command(_ctx(), "git push")
    force = await tools.inspect_command(_ctx(), "git push --force origin main")

    assert "use run_command" in commit
    assert "use run_command" in push
    assert "Refusing" in force  # deny is reported even by inspect_command


@pytest.mark.asyncio
async def test_run_command_argv_only_no_shell(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # A shell metacharacter is just an argv token; no shell expansion happens.
    # `true` is read-only (auto), so this runs without approval and the pipe is
    # passed verbatim as an argument rather than chaining commands.
    result = await tools.run_command(_ctx(approved=False), "true | rm -rf /")
    assert "$ true | rm -rf /" in result


def test_destructive_tools_require_pydantic_ai_approval():
    for name in ("write_file", "edit_file"):
        tool = tools.TOOL_REGISTRY[name]

        assert isinstance(tool, Tool)
        assert tool.requires_approval is True


def test_run_command_uses_dynamic_approval(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    tool = tools.TOOL_REGISTRY["run_command"]
    # run_command is a sequential Tool but NOT statically requires_approval;
    # approval is decided per call via ApprovalRequired.
    assert isinstance(tool, Tool)
    assert tool.requires_approval is False
    assert tool.sequential is True


def test_read_only_tools_do_not_require_approval():
    for name in ("read_file", "glob", "grep", "inspect_command"):
        assert not isinstance(tools.TOOL_REGISTRY[name], Tool)


@pytest.mark.asyncio
async def test_web_search_no_api_key(monkeypatch):
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    # Clear the lru_cache for _parallel_client to ensure it checks settings again
    tools._parallel_client.cache_clear()

    result = await tools.web_search("test query")
    assert "PARALLEL_API_KEY is not set" in result
