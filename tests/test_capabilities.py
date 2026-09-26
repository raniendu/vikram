"""Spec-driven harness capabilities (vikram/capabilities.py)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from tests.conftest import find_log_event
from vikram.agent import ApprovalRequest, _approval_handler, build_agent
from vikram.capabilities import CapabilitiesSpec, build_capabilities
from vikram.settings import VikramSettings
from vikram.spec import AgentSpecDraft, load_spec
from vikram.spec_io import render_agent_toml

SPEC_ROOT = Path(__file__).resolve().parent.parent / "spec"


@pytest.fixture
def settings(monkeypatch, tmp_path) -> VikramSettings:
    for name in ("VIKRAM_MODEL", "VIKRAM_MODEL_PROVIDER", "VIKRAM_SPEC_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    return VikramSettings(_env_file=None)


def _spec_with(capabilities: dict, name: str = "vikram"):
    spec = load_spec(name, SPEC_ROOT)
    return spec.model_copy(
        update={"capabilities": CapabilitiesSpec.model_validate(capabilities)}
    )


def test_absent_table_builds_nothing():
    spec = _spec_with({})
    assert build_capabilities(spec, surface="cli") == []


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        CapabilitiesSpec.model_validate({"compacton": {}})
    with pytest.raises(ValidationError):
        CapabilitiesSpec.model_validate({"compaction": {"max_tokenz": 5}})


def test_schema_exposes_capabilities_to_the_editor():
    schema = AgentSpecDraft.model_json_schema()
    assert "capabilities" in schema["properties"]


def test_each_entry_builds_the_matching_harness_capability():
    from pydantic_ai_harness import (
        SlidingWindowCompaction,
        SpendLimits,
        SummarizingCompaction,
        ToolOutputLimits,
    )
    from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments

    spec = _spec_with(
        {
            "repair_tool_arguments": True,
            "tool_output_limits": {"max_chars": 1234, "strategy": "tail"},
            "compaction": {"max_tokens": 5000, "keep_messages": 7},
            "spend_limits": {"tokens_per_run": 1000},
        }
    )
    built = build_capabilities(spec, surface="threaded")
    types = [type(c) for c in built]
    assert types == [
        RepairToolArguments,
        ToolOutputLimits,
        SummarizingCompaction,
        SpendLimits,
    ]
    limits = built[1]
    assert limits.bands[0].over == 1234
    assert limits.bands[0].action.max_chars == 1234
    assert limits.bands[0].action.strategy.value == "tail"
    compaction = built[2]
    assert compaction.max_tokens == 5000 and compaction.keep_messages == 7

    sliding = _spec_with({"compaction": {"strategy": "sliding_window"}})
    assert isinstance(
        build_capabilities(sliding, surface="cli")[0], SlidingWindowCompaction
    )


def test_spend_limits_apply_only_on_their_surfaces():
    spec = _spec_with({"spend_limits": {"tokens_per_run": 1000}})
    assert build_capabilities(spec, surface="cli") == []
    assert len(build_capabilities(spec, surface="telegram")) == 1

    everywhere = _spec_with(
        {"spend_limits": {"tokens_per_run": 1000, "surfaces": ["cli"]}}
    )
    assert len(build_capabilities(everywhere, surface="cli")) == 1

    no_budget = _spec_with({"spend_limits": {}})
    assert build_capabilities(no_budget, surface="http") == []


def test_capabilities_round_trip_through_the_spec_writer():
    path = SPEC_ROOT / "vikram" / "agent.toml"
    original = path.read_text()
    draft = AgentSpecDraft.model_validate(tomllib.loads(original))
    empty = draft.model_copy(update={"capabilities": CapabilitiesSpec()})
    assert "[capabilities" not in render_agent_toml(empty)

    updated = draft.model_copy(
        update={
            "capabilities": CapabilitiesSpec.model_validate(
                {
                    "repair_tool_arguments": True,
                    "compaction": {"max_tokens": 9000},
                    "spend_limits": {"usd_per_day": "2.5"},
                }
            )
        }
    )
    text = render_agent_toml(updated, existing=original)
    assert "[capabilities.compaction]" in text
    reparsed = AgentSpecDraft.model_validate(tomllib.loads(text))
    assert reparsed.capabilities == updated.capabilities


def test_build_agent_attaches_capabilities_after_approvals(settings, log_events):
    spec = _spec_with({"repair_tool_arguments": True, "compaction": {}})
    agent = build_agent(spec=spec, settings=settings, surface="cli")

    root = agent.raw_agent.root_capability
    names = [type(c).__name__ for c in getattr(root, "capabilities", [root])]
    assert names.index("HandleDeferredToolCalls") < names.index("RepairToolArguments")
    assert "SummarizingCompaction" in names

    built = find_log_event(log_events, "agent_built")
    assert built["capabilities"] == ["RepairToolArguments", "SummarizingCompaction"]


def test_pydantic_ai_banner_is_disabled():
    """The CLI and ACP own stdout; a first-run banner would corrupt it."""
    import pydantic_ai

    import vikram.agent  # noqa: F401

    assert pydantic_ai.BANNER_ENABLED is False


async def test_tool_guardrail_approval_reaches_vikrams_approval_handler():
    """The bridge later work streams rely on: a harness ToolGuardrail that
    returns ``approve()`` must go through Vikram's own approval prompt."""
    from pydantic_ai_harness import GuardrailResult, ToolGuardrail

    asked: list[ApprovalRequest] = []

    def approval_request(request: ApprovalRequest) -> str:
        asked.append(request)
        return "yes" if request.args["path"] == "ok.txt" else "no"

    def model(messages, info):
        returned = [
            p
            for m in messages
            for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(
                parts=[
                    ToolCallPart("write", {"path": "ok.txt"}),
                    ToolCallPart("write", {"path": "no.txt"}),
                    ToolCallPart("blocked", {"path": "x"}),
                ]
            )
        text = " | ".join(f"{r.tool_name}:{r.content}" for r in returned)
        return ModelResponse(parts=[TextPart(text)])

    def guard(call):
        if call.name == "write":
            return GuardrailResult.approve()
        if call.name == "blocked":
            return GuardrailResult.block("refused by policy")
        return GuardrailResult.allow()

    agent = Agent(
        FunctionModel(model),
        capabilities=[
            HandleDeferredToolCalls(
                handler=_approval_handler(
                    surface="cli",
                    approve_all=False,
                    approval_ask=None,
                    approval_request=approval_request,
                )
            ),
            ToolGuardrail(guard=guard),
        ],
    )

    @agent.tool_plain
    def write(path: str) -> str:
        return f"wrote {path}"

    @agent.tool_plain
    def blocked(path: str) -> str:
        return "should not run"

    result = await agent.run("go")

    assert sorted(r.args["path"] for r in asked) == ["no.txt", "ok.txt"]
    assert "write:wrote ok.txt" in result.output
    assert "wrote no.txt" not in result.output
    assert "refused by policy" in result.output
    assert "should not run" not in result.output


def test_editor_save_keeps_comments_inside_capability_tables():
    original = (
        'name = "X"\ndescription = "d"\nsystem_prompt = "p.md"\n\n'
        "[capabilities]\n"
        "repair_tool_arguments = true   # keep me\n\n"
        "[capabilities.compaction]\n"
        "max_tokens = 24000             # and me\n"
        '# strategy = "sliding_window"  # and this option\n'
    )
    draft = AgentSpecDraft.model_validate(tomllib.loads(original))
    changed = draft.model_copy(
        update={
            "capabilities": CapabilitiesSpec.model_validate(
                {"repair_tool_arguments": True, "compaction": {"max_tokens": 9000}}
            )
        }
    )
    text = render_agent_toml(changed, existing=original)
    assert "# keep me" in text
    assert '# strategy = "sliding_window"  # and this option' in text
    assert "max_tokens = 9000" in text


# --- WS1: the capabilities do their job inside a Vikram-built agent -----------


def _tool_returns(messages):
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]


def _plain_agent(capabilities: dict, settings, tools):
    """A Vikram-built agent (real capability wiring) with extra test tools."""
    spec = _spec_with(capabilities)
    agent = build_agent(spec=spec, settings=settings, surface="cli")
    for tool in tools:
        agent.raw_agent.tool_plain(tool)
    return agent


def test_shipped_specs_enable_the_quick_wins():
    for name in ("coder", "vikram"):
        caps = load_spec(name, SPEC_ROOT).capabilities
        assert caps.repair_tool_arguments is True
        assert caps.tool_output_limits is not None
        assert caps.compaction is not None
        assert caps.spend_limits is None  # supported, deliberately off


async def test_malformed_tool_arguments_are_repaired(settings):
    calls: list[str] = []

    def echo(path: str) -> str:
        calls.append(path)
        return f"echo {path}"

    def model(messages, info):
        if not _tool_returns(messages):
            # Trailing comma and single quotes: invalid JSON.
            return ModelResponse(parts=[ToolCallPart("echo", "{'path': 'a.txt',}")])
        return ModelResponse(parts=[TextPart("done")])

    agent = _plain_agent({"repair_tool_arguments": True}, settings, [echo])
    with agent.raw_agent.override(model=FunctionModel(model)):
        result = await agent.run("go")

    assert calls == ["a.txt"]
    assert result.output == "done"


async def test_oversized_tool_output_is_truncated_before_the_model(settings):
    seen: list[int] = []

    def big() -> str:
        return "x" * 100_000

    def model(messages, info):
        returned = _tool_returns(messages)
        if not returned:
            return ModelResponse(parts=[ToolCallPart("big", {})])
        seen.append(len(returned[0].model_response_str()))
        return ModelResponse(parts=[TextPart("done")])

    agent = _plain_agent({"tool_output_limits": {"max_chars": 2000}}, settings, [big])
    with agent.raw_agent.override(model=FunctionModel(model)):
        await agent.run("go")

    assert seen and seen[0] <= 2000


async def test_sliding_window_compaction_trims_long_history(settings):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    history = []
    for i in range(60):
        history.append(ModelRequest(parts=[UserPromptPart(f"question {i} " * 50)]))
        history.append(ModelResponse(parts=[TextPart(f"answer {i} " * 50)]))
    sizes: list[int] = []

    def model(messages, info):
        sizes.append(len(messages))
        return ModelResponse(parts=[TextPart("ok")])

    agent = _plain_agent(
        {
            "compaction": {
                "strategy": "sliding_window",
                "max_tokens": 2000,
                "keep_messages": 10,
            }
        },
        settings,
        [],
    )
    with agent.raw_agent.override(model=FunctionModel(model)):
        await agent.run("latest question", message_history=history)

    assert sizes and sizes[0] < len(history) + 1


async def test_token_budget_stops_a_run(settings):
    from pydantic_ai_harness.spend import SpendLimitExceeded

    def model(messages, info):
        if len(_tool_returns(messages)) < 5:
            return ModelResponse(parts=[ToolCallPart("step", {})])
        return ModelResponse(parts=[TextPart("done")])

    def step() -> str:
        return "tick " * 200

    spec = _spec_with({"spend_limits": {"tokens_per_run": 50}})
    agent = build_agent(spec=spec, settings=settings, surface="threaded")
    agent.raw_agent.tool_plain(step)
    with agent.raw_agent.override(model=FunctionModel(model)):
        with pytest.raises(SpendLimitExceeded):
            await agent.run("go")
