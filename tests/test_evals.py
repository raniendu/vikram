"""Offline tests for the eval suite machinery (evals/).

No model server is contacted: agent runs use pydantic-ai's FunctionModel and
TestModel, Ollama digests are patched, and git work happens in temp repos.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from evals import SUITE_DIR, changes, compare, gitutil, history, orchestrate, report
from evals import runner as eval_runner
from evals.cases import CaseSpec, CheckSpec, SetupSpec, list_agents, load_suite
from evals.checks import (
    Observation,
    ToolCall,
    ToolResult,
    check_names,
    run_check,
    snapshot_workspace,
)
from evals.judge import JudgeModel
from tests.conftest import find_log_event
from vikram.settings import VikramSettings

REPO_ROOT = Path(__file__).resolve().parent.parent

_ENV_VARS = (
    "VIKRAM_MODEL",
    "VIKRAM_MODEL_PROVIDER",
    "VIKRAM_SPEC_ROOT",
    "OLLAMA_BASE_URL",
    "PARALLEL_API_KEY",
)


@pytest.fixture
def settings(monkeypatch, tmp_path) -> VikramSettings:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    return VikramSettings(_env_file=None)


@pytest.fixture
def git_identity(monkeypatch) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Eval Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "eval-test@localhost")
    monkeypatch.delenv(gitutil.SKIP_ENV, raising=False)
    monkeypatch.delenv(orchestrate.DISABLE_ENV, raising=False)
    monkeypatch.delenv(orchestrate.AUTOCOMMIT_ENV, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


# --- the committed suite is valid ------------------------------------------


def test_committed_suites_load_and_use_known_checks():
    agents = list_agents()
    assert {"coder", "vikram"} <= set(agents)
    known = set(check_names())
    for agent in agents:
        suite = load_suite(agent)
        assert suite.cases
        for case in suite.cases:
            assert case.id.startswith(f"{agent}.")
            assert case.checks or case.judge, case.id
            for check in case.checks:
                assert check.type in known, (case.id, check.type)
            if case.workspace:
                assert (SUITE_DIR / "fixtures" / case.workspace).is_dir()


def test_fixture_repo_has_exactly_one_failing_test(tmp_path):
    root = tmp_path / "ws"
    eval_runner.prepare_workspace(root, "mini_repo", SetupSpec())
    obs = Observation(output="", workspace=root)
    assert not run_check(CheckSpec(type="tests_pass"), obs).passed
    assert run_check(
        CheckSpec(type="tests_pass", paths=["tests/test_stock.py"]), obs
    ).passed

    dates = root / "inventory" / "dates.py"
    dates.write_text(dates.read_text().replace(") > parse_date", ") >= parse_date"))
    assert run_check(CheckSpec(type="tests_pass"), obs).passed


# --- checks ----------------------------------------------------------------


def test_output_and_tool_checks():
    obs = Observation(
        output="The function is_expired lives in inventory/dates.py.",
        tool_calls=[ToolCall("run_command", '{"command": "python -m pytest -q"}')],
        tool_results=[ToolResult("read_file", "Refusing: sensitive path")],
        today=date(2026, 9, 26),
    )
    ok = lambda **kw: run_check(CheckSpec(**kw), obs).passed  # noqa: E731
    assert ok(type="output_contains", values=["IS_EXPIRED", "dates.py"])
    assert not ok(type="output_contains", values=["is_expired", "stock.py"])
    assert ok(type="output_not_contains", value="stock.py")
    assert ok(type="output_matches", pattern=r"inventory/\w+\.py")
    assert ok(type="tool_called", tool="run_command", args_contains="pytest")
    assert not ok(type="tool_called", tool="run_command", args_contains="curl")
    assert ok(type="tool_not_called", any_of=["write_file", "edit_file"])
    assert ok(type="tool_result_contains", tool="read_file", value="refusing")
    assert ok(type="no_tool_result_contains", value="root:x:0:0")
    assert not ok(type="mentions_today")

    obs.output = "Today is September 26, 2026."
    assert ok(type="mentions_today")
    obs.output = "2026-09-26"
    assert ok(type="mentions_today")


def test_check_labels_are_specific():
    obs = Observation(output="x")
    result = run_check(CheckSpec(type="output_contains", value="x"), obs)
    assert result.name == "output_contains:x"


def test_workspace_checks(tmp_path):
    root = tmp_path / "ws"
    eval_runner.prepare_workspace(root, "mini_repo", SetupSpec())
    obs = Observation(output="", workspace=root, initial_files=snapshot_workspace(root))
    assert run_check(CheckSpec(type="files_unchanged"), obs).passed

    (root / "inventory" / "stock.py").write_text("changed\n")
    assert not run_check(CheckSpec(type="files_unchanged"), obs).passed
    assert run_check(CheckSpec(type="files_unchanged", paths=["tests/*"]), obs).passed
    assert not run_check(
        CheckSpec(type="workspace_not_contains", value="compute_total", paths=["*.py"]),
        obs,
    ).passed
    assert run_check(
        CheckSpec(type="file_contains", path="inventory/stock.py", value="changed"),
        obs,
    ).passed
    hidden = CheckSpec(
        type="hidden_test",
        code="from inventory.dates import parse_date\n\n"
        "def test_x():\n    assert parse_date('2024-01-02').day == 2\n",
    )
    assert run_check(hidden, obs).passed
    assert not (root / "_eval_hidden_test.py").exists()


def test_prepare_workspace_setup_steps(tmp_path):
    root = tmp_path / "ws"
    setup = SetupSpec.model_validate(
        {
            "git_init": True,
            "write": {"inventory/dates.py": "changed\n"},
            "generate": [
                {
                    "path": "logs/a.log",
                    "lines": 5,
                    "line": "ok {n}",
                    "inject": {3: "ERR"},
                }
            ],
        }
    )
    eval_runner.prepare_workspace(root, "mini_repo", setup)
    assert (root / "logs" / "a.log").read_text().splitlines() == [
        "ok 1",
        "ok 2",
        "ERR",
        "ok 4",
        "ok 5",
    ]
    status = _git(root, "status", "--porcelain")
    assert " M inventory/dates.py" in status


def test_eval_approvals_allow_test_runs_only():
    request = lambda tool, **args: type(  # noqa: E731
        "R", (), {"tool_name": tool, "args": args}
    )()
    assert eval_runner._approve(request("write_file", path="a")) == "yes"
    assert (
        eval_runner._approve(request("run_command", command="python -m pytest"))
        == "yes"
    )
    assert (
        eval_runner._approve(request("run_command", command="cat /etc/passwd")) == "no"
    )
    assert eval_runner._approve(request("run_command", command="curl x")) == "no"
    assert eval_runner._approve(request("some_other_tool")) == "no"


# --- runner (offline model) -------------------------------------------------


class _Overridden:
    """A built VikramAgent whose runs use a scripted model."""

    def __init__(self, agent: Any, model: Any) -> None:
        self._agent = agent
        self._model = model
        self.mcp_clients = agent.mcp_clients

    async def run(self, prompt: str, **kwargs: Any) -> Any:
        with self._agent.raw_agent.override(model=self._model):
            return await self._agent.run(prompt, **kwargs)


def _scripted_coder(messages: list[Any], info: Any) -> ModelResponse:
    returned = [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    prompt = str(messages[0].parts[-1].content)
    if not returned:
        path = ".env" if ".env" in prompt else "inventory/dates.py"
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": path})])
    if ".env" in prompt:
        return ModelResponse(parts=[TextPart("I can't read secret files.")])
    return ModelResponse(parts=[TextPart("is_expired, in inventory/dates.py.")])


@pytest.fixture
def offline_models(monkeypatch):
    monkeypatch.setattr(
        eval_runner, "ollama_digest", lambda base_url, model: "sha256:test"
    )
    from vikram.agent import build_agent as real_build

    def build(spec, settings, **kwargs):
        return _Overridden(
            real_build(spec, settings, **kwargs), FunctionModel(_scripted_coder)
        )

    return build


async def test_run_suite_offline_aggregates_metrics(settings, offline_models, tmp_path):
    result = await eval_runner.run_suite(
        "coder",
        repeats=2,
        case_filter=["coder.find_expiry_logic", "coder.refuse_env_secrets"],
        details_dir=tmp_path / "details",
        settings=settings,
        build_agent=offline_models,
    )
    assert result["agent"] == "coder"
    assert result["env"]["provider"] == "ollama"
    assert result["env"]["model_digest"] == "sha256:test"
    by_id = {case["id"]: case for case in result["cases"]}
    find = by_id["coder.find_expiry_logic"]
    assert find["pass_rate"] == 1.0
    assert find["repeats"] == 2
    assert find["tool_calls_mean"] == 1.0
    assert find["tokens_mean"] > 0
    assert by_id["coder.refuse_env_secrets"]["pass_rate"] == 1.0
    assert result["summary"]["pass_rate"] == 1.0

    detail = json.loads(
        (tmp_path / "details" / "coder.find_expiry_logic.json").read_text()
    )
    assert detail["repeats"][0]["trace"][0]["tool"] == "read_file"


async def test_run_suite_skips_needs_web_without_key(settings, offline_models):
    result = await eval_runner.run_suite(
        "vikram",
        repeats=1,
        case_filter=["vikram.research_uses_skill"],
        settings=settings,
        build_agent=offline_models,
    )
    assert result["cases"] == [
        {"id": "vikram.research_uses_skill", "status": "skipped", "reason": "needs_web"}
    ]
    assert result["summary"]["skipped"] == 1


async def test_judge_scores_feed_pass_rate(settings, monkeypatch):
    monkeypatch.setattr(eval_runner, "ollama_digest", lambda *a: None)
    from vikram.agent import build_agent as real_build

    answer = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("Processes vs threads.")])
    )
    judge_model = JudgeModel(
        raw=TestModel(
            custom_output_args={"reason": "fine", "pass": True, "score": 0.4}
        ),
        provider="test",
        model="judge",
    )
    result = await eval_runner.run_suite(
        "vikram",
        repeats=1,
        case_filter=["vikram.explain_process_vs_thread"],
        settings=settings,
        judge_model=judge_model,
        build_agent=lambda sp, st, **kw: _Overridden(real_build(sp, st, **kw), answer),
    )
    case = result["cases"][0]
    assert case["judge_mean"] == 0.4
    assert case["pass_rate"] == 0.0  # below the 0.7 threshold
    assert result["env"]["judge_model"] == "test:judge"


async def test_run_errors_are_recorded_not_raised(settings, monkeypatch, log_events):
    monkeypatch.setattr(eval_runner, "ollama_digest", lambda *a: None)

    def broken(messages, info):
        raise RuntimeError("model exploded")

    from vikram.agent import build_agent as real_build

    result = await eval_runner.run_suite(
        "coder",
        repeats=1,
        case_filter=["coder.find_expiry_logic"],
        settings=settings,
        build_agent=lambda s, st, **kw: _Overridden(
            real_build(s, st, **kw), FunctionModel(broken)
        ),
    )
    case = result["cases"][0]
    assert case["errors"] == 1
    assert case["error_types"] == ["RuntimeError"]
    assert case["pass_rate"] == 0.0
    failed = find_log_event(log_events, "eval_case_run_failed")
    assert failed["error_type"] == "RuntimeError"


# --- change detection --------------------------------------------------------

_AGENT_TOML = """\
name = "Coder"
system_prompt = "system_prompt.md"
model_provider = "ollama"
model = "qwen"
tools = ["read_file", "grep"]

[model_settings]
temperature = 0.2
"""


@pytest.fixture
def spec_repo(tmp_path, git_identity) -> Path:
    repo = tmp_path / "repo"
    (repo / "spec" / "coder").mkdir(parents=True)
    (repo / "spec" / "vikram").mkdir(parents=True)
    (repo / "spec" / "shared" / "context").mkdir(parents=True)
    (repo / "spec" / "coder" / "agent.toml").write_text(_AGENT_TOML)
    (repo / "spec" / "coder" / "system_prompt.md").write_text("Be careful.\n")
    (repo / "spec" / "vikram" / "system_prompt.md").write_text("Be helpful.\n")
    (repo / "spec" / "shared" / "context" / "production.md").write_text("Prod.\n")
    (repo / "README.md").write_text("readme\n")
    _git(repo, "init", "-q", "-b", "main")
    _commit_all(repo, "initial")
    return repo


def _detect(repo: Path, edit) -> changes.ChangeSet:
    base = _git(repo, "rev-parse", "HEAD").strip()
    edit(repo)
    head = _commit_all(repo, "edit")
    return changes.detect_changes(repo, base, head, ["coder", "vikram"])


def test_detects_prompt_change_for_one_agent(spec_repo):
    cs = _detect(
        spec_repo,
        lambda r: (r / "spec/coder/system_prompt.md").write_text(
            "Be careful.\nAnd brief.\n"
        ),
    )
    assert cs.agents() == ["coder"]
    described = cs.for_agent("coder")
    assert described["kinds"] == ["prompt"]
    assert described["details"]["prompt_lines"] == {"added": 1, "removed": 0}


def test_detects_model_settings_and_tools_with_details(spec_repo):
    def edit(repo: Path) -> None:
        text = _AGENT_TOML.replace("temperature = 0.2", "temperature = 0.1")
        text = text.replace('"grep"]', '"grep", "run_command"]')
        text = text.replace('model = "qwen"', 'model = "qwen:v2"')
        (repo / "spec/coder/agent.toml").write_text(text)

    described = _detect(spec_repo, edit).for_agent("coder")
    assert described["kinds"] == ["model", "model_settings", "tools"]
    assert described["details"]["model_settings.temperature"] == {
        "from": 0.2,
        "to": 0.1,
    }
    assert described["details"]["tools"] == {"added": ["run_command"], "removed": []}
    assert described["details"]["model"] == {"from": "qwen", "to": "qwen:v2"}


def test_shared_context_affects_every_agent(spec_repo):
    cs = _detect(
        spec_repo,
        lambda r: (r / "spec/shared/context/production.md").write_text("Prod v2.\n"),
    )
    assert cs.agents() == ["coder", "vikram"]


def test_unrelated_and_history_changes_do_not_trigger(spec_repo):
    def edit(repo: Path) -> None:
        (repo / "README.md").write_text("new readme\n")
        (repo / "evals" / "history").mkdir(parents=True)
        (repo / "evals" / "history" / "x.json").write_text("{}")

    assert _detect(spec_repo, edit).agents() == []


def test_framework_version_bump_is_detected(spec_repo):
    lock = '[[package]]\nname = "pydantic-ai-slim"\nversion = "{v}"\n'
    (spec_repo / "uv.lock").write_text(lock.format(v="2.31.1"))
    _commit_all(spec_repo, "lock")
    cs = _detect(
        spec_repo, lambda r: (r / "uv.lock").write_text(lock.format(v="2.40.0"))
    )
    assert cs.for_agent("vikram")["details"]["pydantic-ai-slim"] == {
        "from": "2.31.1",
        "to": "2.40.0",
    }


def test_model_digest_change_is_recorded():
    cs = changes.ChangeSet(base="a", head="b")
    changes.add_model_version_change(cs, "coder", "qwen", "sha256:1", "sha256:1")
    assert cs.agents() == []
    changes.add_model_version_change(cs, "coder", "qwen", "sha256:1", "sha256:2")
    assert cs.for_agent("coder")["kinds"] == ["model_version"]


# --- history -------------------------------------------------------------------


def _result(agent: str, pass_rates: dict[str, float], *, suite: str = "s1") -> dict:
    cases = [
        {
            "id": case_id,
            "status": "ok",
            "pass_rate": rate,
            "judge_mean": None,
            "tokens_mean": 1000.0,
            "latency_p50_ms": 2000.0,
            "tool_calls_mean": 2.0,
            "failed_checks": {},
            "errors": 0,
        }
        for case_id, rate in pass_rates.items()
    ]
    return {
        "agent": agent,
        "suite_hash": suite,
        "repeats": 3,
        "env": {"provider": "ollama", "model": "qwen", "model_digest": "sha256:1"},
        "summary": eval_runner.summarize(cases),
        "cases": cases,
    }


def test_delta_flags_worse_cases_beyond_noise():
    before = _result("coder", {"a": 1.0, "b": 1.0, "c": 0.67})
    after = _result("coder", {"a": 0.33, "b": 0.67, "c": 1.0})
    delta = history.compute_delta(after, before)
    assert delta["cases"]["a"]["status"] == "worse"
    assert delta["cases"]["b"]["status"] == "same"  # one flipped repeat = noise
    assert delta["cases"]["c"]["status"] == "same"
    assert delta["summary"]["worse_cases"] == ["a"]
    assert delta["summary"]["pass_rate"] == pytest.approx(-0.2233, abs=1e-3)


def test_baseline_is_newest_ancestor_with_same_suite(spec_repo):
    first = _git(spec_repo, "rev-parse", "HEAD").strip()
    (spec_repo / "a.txt").write_text("a")
    second = _commit_all(spec_repo, "second")
    (spec_repo / "b.txt").write_text("b")
    head = _commit_all(spec_repo, "head")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    records = []
    for offset, (sha, suite) in enumerate(
        [(first, "s1"), (second, "s2"), (head, "s1")]
    ):
        records.append(
            history.build_record(
                repo=spec_repo,
                result=_result("coder", {"a": 1.0}, suite=suite),
                sha=sha,
                trigger="commit",
                change={"kinds": []},
                baseline=None,
                when=now + timedelta(minutes=offset),
            )
        )
    found = history.find_baseline(
        spec_repo, records, agent="coder", head=head, suite_hash="s1"
    )
    assert found["commit"]["sha"] == first  # skips other suite and HEAD itself
    assert (
        history.find_baseline(
            spec_repo, records, agent="vikram", head=head, suite_hash="s1"
        )
        is None
    )


def test_records_round_trip_without_outputs(spec_repo):
    sha = _git(spec_repo, "rev-parse", "HEAD").strip()
    record = history.build_record(
        repo=spec_repo,
        result=_result("coder", {"a": 1.0}),
        sha=sha,
        trigger="commit",
        change={"kinds": ["prompt"], "files": [], "details": {}},
        baseline=None,
        when=datetime(2026, 9, 26, 10, 15, 2, tzinfo=timezone.utc),
    )
    path = history.write_record(spec_repo, record)
    assert path.relative_to(spec_repo).as_posix() == (
        f"evals/history/2026/09/2026-09-26T10-15-02_{sha[:7]}_coder.json"
    )
    assert history.load_records(spec_repo) == [record]
    text = path.read_text()
    assert '"output"' not in text and '"prompt":' not in text
    assert "coder @" in history.one_line_summary(record)


# --- the full pipeline ------------------------------------------------------------


@pytest.fixture
def eval_repo(tmp_path, git_identity) -> Path:
    """A git repo carrying the real suite (cases, fixtures, scoring code)."""
    repo = tmp_path / "evalrepo"
    repo.mkdir()
    shutil.copytree(
        SUITE_DIR,
        repo / "evals",
        ignore=shutil.ignore_patterns("history", "__pycache__"),
    )
    (repo / "spec" / "coder").mkdir(parents=True)
    (repo / "spec" / "coder" / "system_prompt.md").write_text("v1\n")
    (repo / ".gitignore").write_text(".vikram/\n")
    _git(repo, "init", "-q", "-b", "main")
    _commit_all(repo, "initial")
    (repo / "spec" / "coder" / "system_prompt.md").write_text("v2\n")
    _commit_all(repo, "tweak coder prompt")
    return repo


def _fake_runner(pass_rate: float):
    calls: list[tuple[str, str]] = []

    def run(repo, code_dir, suite_root, agent, *, repeats, run_id):
        sha = _git(code_dir, "rev-parse", "HEAD").strip()
        calls.append((agent, sha))
        from evals.cases import suite_hash

        result = _result(agent, {"coder.x": pass_rate})
        result["suite_hash"] = suite_hash(agent, suite_root / "evals")
        return result

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_process_job_builds_baseline_then_reuses_it(eval_repo):
    head = _git(eval_repo, "rev-parse", "HEAD").strip()
    parent = _git(eval_repo, "rev-parse", "HEAD~1").strip()
    # Something unrelated the user has staged must stay staged, uncommitted.
    (eval_repo / "notes.txt").write_text("wip\n")
    _git(eval_repo, "add", "notes.txt")

    runner = _fake_runner(0.5)
    job = orchestrate.Job(sha=head, branch="main", agents=["coder"])
    written = orchestrate.process_job(eval_repo, job, repeats=1, runner=runner)

    assert runner.calls == [("coder", parent), ("coder", head)]
    assert len(written) == 2
    records = history.load_records(eval_repo, "coder")
    baseline, after = records
    assert baseline["trigger"] == "baseline"
    assert baseline["commit"]["sha"] == parent
    assert after["baseline_run_id"] == baseline["run_id"]
    assert after["change"]["kinds"] == ["prompt"]
    assert after["change"]["base"] == parent
    assert after["delta"]["summary"]["pass_rate"] == 0.0

    log = _git(eval_repo, "log", "-1", "--format=%s", "--name-only")
    assert log.startswith(f"evals: coder results for {head[:7]} (prompt)")
    assert "notes.txt" not in log
    assert "A  notes.txt" in _git(eval_repo, "status", "--porcelain")
    assert _git(eval_repo, "worktree", "list").count("\n") == 1  # cleaned up

    # Next change: the committed record is the baseline, no parent run needed.
    _git(eval_repo, "reset", "-q", "notes.txt")
    (eval_repo / "spec" / "coder" / "system_prompt.md").write_text("v3\n")
    _git(eval_repo, "add", "spec")
    head2 = _commit_all(eval_repo, "tweak again")
    runner2 = _fake_runner(1.0)
    orchestrate.process_job(
        eval_repo,
        orchestrate.Job(sha=head2, branch="main", agents=["coder"]),
        repeats=1,
        runner=runner2,
    )
    assert runner2.calls == [("coder", head2)]
    latest = history.load_records(eval_repo, "coder")[-1]
    assert latest["baseline_run_id"] == after["run_id"]
    assert latest["delta"]["summary"]["pass_rate"] == 0.5
    assert latest["delta"]["cases"]["coder.x"]["status"] == "better"


def test_process_job_leaves_records_uncommitted_when_disabled(eval_repo, monkeypatch):
    monkeypatch.setenv(orchestrate.AUTOCOMMIT_ENV, "0")
    head = _git(eval_repo, "rev-parse", "HEAD").strip()
    before = _git(eval_repo, "rev-parse", "HEAD").strip()
    orchestrate.process_job(
        eval_repo,
        orchestrate.Job(sha=head, branch="main", agents=["coder"]),
        repeats=1,
        runner=_fake_runner(1.0),
    )
    assert _git(eval_repo, "rev-parse", "HEAD").strip() == before
    monkeypatch.delenv(orchestrate.AUTOCOMMIT_ENV)
    assert orchestrate.record_uncommitted(eval_repo)
    assert _git(eval_repo, "log", "-1", "--format=%s").startswith("evals: record")


def test_model_unavailable_writes_nothing(eval_repo):
    def unavailable(*args, **kwargs):
        raise orchestrate.ModelUnavailable("down")

    head = _git(eval_repo, "rev-parse", "HEAD").strip()
    written = orchestrate.process_job(
        eval_repo,
        orchestrate.Job(sha=head, branch="main", agents=["coder"]),
        repeats=1,
        runner=unavailable,
    )
    assert written == []
    summary = orchestrate.summary_log_path(eval_repo).read_text()
    assert "SKIPPED" in summary


def test_hook_queues_relevant_commits_only(eval_repo, monkeypatch):
    spawned: list[Path] = []
    monkeypatch.setattr(orchestrate, "spawn_worker", spawned.append)

    job = orchestrate.hook(eval_repo)
    assert job is not None and job.agents == ["coder"]
    assert spawned == [eval_repo]

    (eval_repo / "README.md").write_text("docs only\n")
    _commit_all(eval_repo, "docs")
    orchestrate._pending_path(eval_repo).unlink()
    assert orchestrate.hook(eval_repo) is None

    (eval_repo / "spec" / "coder" / "system_prompt.md").write_text("v9\n")
    _commit_all(eval_repo, "prompt again")
    monkeypatch.setenv(gitutil.SKIP_ENV, "1")
    assert orchestrate.hook(eval_repo) is None
    monkeypatch.delenv(gitutil.SKIP_ENV)

    (eval_repo / ".git" / "rebase-merge").mkdir()
    assert orchestrate.hook(eval_repo) is None  # replayed commits are ignored
    (eval_repo / ".git" / "rebase-merge").rmdir()
    assert orchestrate.hook(eval_repo) is not None


def test_evals_overlay_matches_the_commits_framework(tmp_path):
    lock = '[[package]]\nname = "{n}"\nversion = "{v}"\n'
    (tmp_path / "uv.lock").write_text(lock.format(n="pydantic-ai-slim", v="2.20.0"))
    assert orchestrate._evals_overlay(tmp_path) == ["--with", "pydantic-evals==2.20.0"]
    (tmp_path / "uv.lock").write_text(
        lock.format(n="pydantic-ai-slim", v="2.31.1")
        + lock.format(n="pydantic-evals", v="2.31.1")
    )
    assert orchestrate._evals_overlay(tmp_path) == []


def test_enqueue_coalesces_agents(eval_repo):
    orchestrate.enqueue(
        eval_repo, orchestrate.Job(sha="a" * 40, branch="main", agents=["coder"])
    )
    job = orchestrate.enqueue(
        eval_repo,
        orchestrate.Job(
            sha="b" * 40, branch="main", agents=["vikram"], trigger="model_check"
        ),
    )
    assert job.sha == "b" * 40
    assert job.agents == ["coder", "vikram"]
    assert job.trigger == "commit"
    popped = orchestrate._pop_job(eval_repo)
    assert popped is not None and popped.agents == ["coder", "vikram"]
    assert orchestrate._pop_job(eval_repo) is None


# --- views ------------------------------------------------------------------------


def test_compare_lists_worse_cases_first():
    before = _result("coder", {"a": 1.0, "b": 0.0})
    after = _result("coder", {"a": 0.0, "b": 1.0})
    text = compare.render(before, after)
    lines = [line for line in text.splitlines() if line.startswith(("a ", "b "))]
    assert lines[0].startswith("a ") and "WORSE" in lines[0]
    assert "better" in lines[1]
    assert "pass rate" in text

    after["suite_hash"] = "other"
    assert compare.render(before, after).startswith("WARNING")


def test_report_renders_history(spec_repo):
    sha = _git(spec_repo, "rev-parse", "HEAD").strip()
    base = history.build_record(
        repo=spec_repo,
        result=_result("coder", {"a": 0.5}),
        sha=sha,
        trigger="baseline",
        change={"kinds": []},
        baseline=None,
        when=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    after = history.build_record(
        repo=spec_repo,
        result=_result("coder", {"a": 1.0}),
        sha=sha,
        trigger="commit",
        change={"kinds": ["prompt"], "details": {"prompt_lines": {"added": 1}}},
        baseline=base,
        when=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    html = report.render([base, after])
    assert "<svg" in html and "coder" in html
    assert "prompt_lines" in html
    assert "+50pt" in html
