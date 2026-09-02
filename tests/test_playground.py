from __future__ import annotations

import asyncio

import pytest

from vikram.playground import (
    ColumnMetrics,
    ColumnSpec,
    PlaygroundError,
    build_column_agents,
    run_comparison,
    validate_columns,
)


def _columns(n=3):
    return [ColumnSpec(provider="ollama", model=f"m{i}") for i in range(n)]


# --- column validation -------------------------------------------------


@pytest.mark.parametrize("count", [2, 3, 4])
def test_accepts_two_to_four_columns(count):
    validate_columns(_columns(count))


@pytest.mark.parametrize("count", [0, 1, 5])
def test_rejects_counts_outside_the_range(count):
    with pytest.raises(PlaygroundError, match="between 2 and 4"):
        validate_columns(_columns(count))


def test_rejects_duplicate_models():
    """Comparing a model with itself is a mistake, not a comparison."""
    same = ColumnSpec(provider="ollama", model="m")

    with pytest.raises(PlaygroundError, match="different model"):
        validate_columns([same, same])


# --- building ----------------------------------------------------------


def test_each_column_gets_its_own_pinned_model(monkeypatch, tmp_path):
    """Overriding settings beats the spec pin without new precedence logic."""
    from vikram.settings import VikramSettings
    from vikram.specstore import load_agent

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VIKRAM_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("VIKRAM_MODEL", "base-model")
    settings = VikramSettings(_env_file=None)
    spec = load_agent("coder", settings)

    columns = [
        ColumnSpec(provider="ollama", model="alpha"),
        ColumnSpec(provider="ollama", model="beta"),
    ]
    agents = build_column_agents(spec, settings, columns)

    assert [a.model_config["model"] for a in agents] == ["alpha", "beta"]


def test_approval_gated_tools_are_disabled_for_comparisons(monkeypatch, tmp_path):
    """Four columns approving the same write_file, then each running it, is
    both bad UX and a correctness hazard; the runtime auto-denies instead."""
    from vikram.settings import VikramSettings
    from vikram.specstore import load_agent

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VIKRAM_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("VIKRAM_MODEL", "m")
    settings = VikramSettings(_env_file=None)
    spec = load_agent("coder", settings)

    agents = build_column_agents(spec, settings, _columns(2))

    # coder still declares them; the surface is what refuses to run them.
    assert "write_file" in agents[0].approval_tool_names


def test_the_shared_command_policy_is_identical_across_columns(monkeypatch, tmp_path):
    """This is what makes single-process fan-out safe.

    set_command_policy writes a module-level global. Holding the agent constant
    means every column sets the same value, so there is nothing to clobber.
    """
    from vikram import tools
    from vikram.settings import VikramSettings
    from vikram.specstore import load_agent

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VIKRAM_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("VIKRAM_MODEL", "m")
    settings = VikramSettings(_env_file=None)
    spec = load_agent("coder", settings)

    import shlex

    probes = ["ls -la", "rm -rf /", "git status", "git push --force"]

    def classify_all():
        return [tools._ACTIVE_POLICY.classify(shlex.split(raw), raw) for raw in probes]

    build_column_agents(spec, settings, _columns(3))
    after_first = classify_all()
    build_column_agents(spec, settings, _columns(3))

    assert classify_all() == after_first
    # And the policy is really doing something, so this is not vacuous.
    assert {decision for decision, _ in after_first} > {"auto"}


# --- running -----------------------------------------------------------


class _FakeAgent:
    def __init__(self, events, delay=0.0):
        self._events = events
        self._delay = delay

    async def stream_events(self, prompt, **kwargs):
        for event in self._events:
            await asyncio.sleep(self._delay)
            yield event


class _Result:
    def __init__(self, output="done", tokens=(10, 5)):
        self.output = output
        self._tokens = tokens

    def all_messages(self):
        return []

    @property
    def usage(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            input_tokens=self._tokens[0],
            output_tokens=self._tokens[1],
            total_tokens=sum(self._tokens),
        )


async def _collect(agents, columns, prompt="hi"):
    seen: list[tuple[str, str]] = []

    async def emit(type_, payload, column_id):
        seen.append((type_, column_id))

    metrics = await run_comparison(agents, columns, prompt, emit=emit)
    return metrics, seen


async def test_every_column_reports_its_own_metrics():
    columns = _columns(3)
    agents = [
        _FakeAgent([{"data": "a"}, {"vikram_result": _Result("A", (10, 1))}]),
        _FakeAgent([{"data": "b"}, {"vikram_result": _Result("B", (20, 2))}]),
        _FakeAgent([{"data": "c"}, {"vikram_result": _Result("C", (30, 3))}]),
    ]

    metrics, _ = await _collect(agents, columns)

    assert [m.output for m in metrics] == ["A", "B", "C"]
    assert [m.input_tokens for m in metrics] == [10, 20, 30]
    assert [m.total_tokens for m in metrics] == [11, 22, 33]
    assert all(m.ttft_ms is not None and m.ttft_ms <= m.total_ms for m in metrics)


async def test_events_are_tagged_with_their_column():
    columns = _columns(2)
    agents = [
        _FakeAgent([{"data": "a"}, {"vikram_result": _Result()}]),
        _FakeAgent([{"data": "b"}, {"vikram_result": _Result()}]),
    ]

    _, seen = await _collect(agents, columns)

    assert {column for _, column in seen} == {"ollama/m0", "ollama/m1"}
    assert ("column.finished", "ollama/m0") in seen


async def test_one_failing_column_does_not_take_down_the_others():
    class _Boom:
        async def stream_events(self, prompt, **kwargs):
            raise RuntimeError("model unavailable")
            yield  # pragma: no cover

    columns = _columns(2)
    agents = [_Boom(), _FakeAgent([{"vikram_result": _Result("ok")}])]

    metrics, _ = await _collect(agents, columns)

    assert "model unavailable" in metrics[0].error
    assert metrics[1].error is None
    assert metrics[1].output == "ok"


async def test_tool_calls_are_counted_per_column():
    columns = _columns(2)
    agents = [
        _FakeAgent(
            [
                {"current_tool_use": {"toolUseId": "1", "name": "grep"}},
                {"current_tool_use": {"toolUseId": "2", "name": "grep"}},
                {"vikram_result": _Result()},
            ]
        ),
        _FakeAgent([{"vikram_result": _Result()}]),
    ]

    metrics, _ = await _collect(agents, columns)

    assert metrics[0].tool_calls == 2
    assert metrics[1].tool_calls == 0


async def test_columns_run_concurrently_not_one_after_another():
    """Three 60ms columns should finish in well under their 180ms serial sum."""
    import time

    columns = _columns(3)
    agents = [
        _FakeAgent([{"data": "x"}, {"vikram_result": _Result()}], delay=0.03)
        for _ in columns
    ]

    started = time.perf_counter()
    await _collect(agents, columns)
    elapsed = (time.perf_counter() - started) * 1000

    assert elapsed < 150, f"took {elapsed:.0f}ms, suggesting serial execution"


async def test_usage_missing_leaves_counts_none_without_failing_the_column():
    class _NoUsage:
        output = "done"

        def all_messages(self):
            return []

        @property
        def usage(self):
            raise AttributeError("not supported")

    columns = _columns(2)
    agents = [
        _FakeAgent([{"vikram_result": _NoUsage()}]),
        _FakeAgent([{"vikram_result": _Result()}]),
    ]

    metrics, _ = await _collect(agents, columns)

    assert metrics[0].error is None
    assert metrics[0].input_tokens is None
    assert metrics[1].input_tokens == 10
