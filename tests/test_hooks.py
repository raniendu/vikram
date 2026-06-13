import sys
import types

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel

from vikram.agent import build_agent
from vikram.hooks import (
    HookBlockedError,
    HookConfigError,
    HookedAgent,
    HookSpec,
    HookToolset,
    build_hooks,
    run_hooks,
)
from vikram.settings import VikramSettings
from vikram.spec import AgentSpec, load_spec

# --- Shared helpers ----------------------------------------------------------

VIKRAM_ENV_VARS = (
    "VIKRAM_MODEL",
    "OLLAMA_BASE_URL",
    "VIKRAM_SPEC_ROOT",
    "VIKRAM_AGENT",
    "VIKRAM_MODEL_PROVIDER",
    "VIKRAM_OPENAI_COMPAT_API_KEY",
    "VIKRAM_OPENAI_COMPAT_BASE_URL",
    "OPENAI_API_KEY",
)


def _local_model_settings(monkeypatch, tmp_path) -> VikramSettings:
    for env_var in VIKRAM_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    return VikramSettings(
        _env_file=None,
        VIKRAM_MODEL_PROVIDER="ollama",
        VIKRAM_MODEL="test-model",
        OLLAMA_BASE_URL="http://localhost:11434",
    )


@pytest.fixture
def hook_module():
    """Register a throwaway module that python-transport hooks can import."""
    mod = types.ModuleType("vikram_test_hooks")
    sys.modules["vikram_test_hooks"] = mod
    try:
        yield mod
    finally:
        sys.modules.pop("vikram_test_hooks", None)


class _StubToolset:
    """Minimal stand-in for the wrapped toolset HookToolset delegates to."""

    def __init__(self, return_value="ok"):
        self.return_value = return_value
        self.calls: list[tuple] = []

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append((name, tool_args))
        return self.return_value


# --- build_hooks / compilation -----------------------------------------------


def test_build_hooks_groups_by_event(hook_module):
    hook_module.fn = lambda payload: None
    hooks = build_hooks(
        [
            HookSpec(event="PreToolUse", command="a"),
            HookSpec(event="PostToolUse", command="b"),
            HookSpec(event="UserPromptSubmit", command="c"),
            HookSpec(
                event="Stop", transport="python", entrypoint="vikram_test_hooks:fn"
            ),
        ]
    )
    assert len(hooks.pre) == 1
    assert len(hooks.post) == 1
    assert len(hooks.user_prompt_submit) == 1
    assert len(hooks.stop) == 1
    assert hooks.has_tool_hooks
    assert hooks.has_run_hooks


def test_build_hooks_expands_env(monkeypatch):
    hooks = build_hooks(
        [
            HookSpec(
                event="PreToolUse", command="${BIN}", args=["${ARG}"], env={"K": "${V}"}
            )
        ],
        environ={"BIN": "/usr/bin/guard", "ARG": "x", "V": "secret"},
    )
    (hook,) = hooks.pre
    assert hook.argv == ["/usr/bin/guard", "x"]
    assert hook.env == {"K": "secret"}


@pytest.mark.parametrize(
    "spec, match",
    [
        (HookSpec(event="PreToolUse", transport="command"), "no 'command'"),
        (HookSpec(event="PreToolUse", transport="python"), "no 'entrypoint'"),
        (HookSpec(event="PreToolUse", command="${MISSING}"), "MISSING"),
        (
            HookSpec(event="PreToolUse", transport="python", entrypoint="no_colon"),
            "module:function",
        ),
        (
            HookSpec(
                event="PreToolUse", transport="python", entrypoint="nonexistent_mod:fn"
            ),
            "could not import",
        ),
    ],
)
def test_build_hooks_rejects_bad_specs(spec, match):
    with pytest.raises(HookConfigError, match=match):
        build_hooks([spec], environ={})


def test_build_hooks_rejects_missing_attribute(hook_module):
    with pytest.raises(HookConfigError, match="no attribute"):
        build_hooks(
            [
                HookSpec(
                    event="Stop",
                    transport="python",
                    entrypoint="vikram_test_hooks:nope",
                )
            ]
        )


# --- run_hooks dispatch ------------------------------------------------------


async def test_run_hooks_matcher_filters_by_tool_name(hook_module):
    hook_module.deny = lambda payload: {"decision": "deny", "reason": "no"}
    hooks = build_hooks(
        [
            HookSpec(
                event="PreToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:deny",
                matcher="run_command",
            )
        ]
    )
    blocked = await run_hooks(hooks.pre, {"x": 1}, tool_name="run_command")
    assert blocked.blocked
    skipped = await run_hooks(hooks.pre, {"x": 1}, tool_name="read_file")
    assert not skipped.blocked


async def test_run_hooks_python_return_shapes(hook_module):
    async def adder(payload):
        return "from-async"

    hook_module.adder = adder
    hook_module.none_fn = lambda payload: None
    hook_module.bad_fn = lambda payload: 12345
    hook_module.boom = lambda payload: (_ for _ in ()).throw(ValueError("boom"))

    hooks = build_hooks(
        [
            HookSpec(
                event="PostToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:adder",
            ),
            HookSpec(
                event="PostToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:none_fn",
            ),
            HookSpec(
                event="PostToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:bad_fn",
            ),
            HookSpec(
                event="PostToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:boom",
            ),
        ]
    )
    decision = await run_hooks(hooks.post, {})
    # str return -> context; None/bad/exception are non-blocking and ignored.
    assert decision.context == "from-async"
    assert not decision.blocked


async def test_run_hooks_command_block_and_context(tmp_path):
    script = tmp_path / "guard.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "read -r payload\n"
        'if echo "$payload" | grep -q secret; then\n'
        '  echo "no secrets" >&2\n'
        "  exit 2\n"
        "fi\n"
        'echo \'{"additional_context": "scanned"}\'\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    hooks = build_hooks([HookSpec(event="PreToolUse", command=str(script))])

    blocked = await run_hooks(hooks.pre, {"v": "secret"}, tool_name="t")
    assert blocked.blocked
    assert "no secrets" in blocked.reason

    ok = await run_hooks(hooks.pre, {"v": "fine"}, tool_name="t")
    assert not ok.blocked
    assert ok.context == "scanned"


async def test_run_hooks_command_nonzero_is_non_blocking(tmp_path):
    script = tmp_path / "fail.sh"
    script.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    hooks = build_hooks([HookSpec(event="PreToolUse", command=str(script))])
    decision = await run_hooks(hooks.pre, {}, tool_name="t")
    assert not decision.blocked


async def test_run_hooks_command_missing_binary_is_non_blocking():
    hooks = build_hooks(
        [HookSpec(event="PreToolUse", command="/no/such/binary/exists")]
    )
    decision = await run_hooks(hooks.pre, {}, tool_name="t")
    assert not decision.blocked


# --- HookToolset (Pre/PostToolUse) -------------------------------------------


async def test_hook_toolset_pre_block_raises_model_retry(hook_module):
    hook_module.deny = lambda payload: {
        "decision": "deny",
        "reason": "blocked by policy",
    }
    hooks = build_hooks(
        [
            HookSpec(
                event="PreToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:deny",
            )
        ]
    )
    stub = _StubToolset()
    ts = HookToolset(stub, pre=hooks.pre, agent_name="t")

    with pytest.raises(ModelRetry, match="blocked by policy"):
        await ts.call_tool("run_command", {"command": "rm -rf /"}, None, None)
    assert stub.calls == []  # tool never ran


async def test_hook_toolset_post_appends_context(hook_module):
    hook_module.note = lambda payload: {"additional_context": "audited"}
    hooks = build_hooks(
        [
            HookSpec(
                event="PostToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:note",
            )
        ]
    )
    stub = _StubToolset(return_value="tool output")
    ts = HookToolset(stub, post=hooks.post, agent_name="t")

    result = await ts.call_tool("read_file", {"path": "x"}, None, None)
    assert result == "tool output\n\naudited"


async def test_hook_toolset_post_block_raises(hook_module):
    hook_module.reject = lambda payload: {"decision": "block", "reason": "bad result"}
    hooks = build_hooks(
        [
            HookSpec(
                event="PostToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:reject",
            )
        ]
    )
    ts = HookToolset(_StubToolset(), post=hooks.post, agent_name="t")
    with pytest.raises(ModelRetry, match="bad result"):
        await ts.call_tool("read_file", {"path": "x"}, None, None)


async def test_hook_toolset_pre_passthrough_runs_tool(hook_module):
    hook_module.allow = lambda payload: None
    hooks = build_hooks(
        [
            HookSpec(
                event="PreToolUse",
                transport="python",
                entrypoint="vikram_test_hooks:allow",
            )
        ]
    )
    stub = _StubToolset(return_value="ran")
    ts = HookToolset(stub, pre=hooks.pre, agent_name="t")
    assert await ts.call_tool("read_file", {"path": "x"}, None, None) == "ran"
    assert stub.calls == [("read_file", {"path": "x"})]


# --- HookedAgent (UserPromptSubmit / Stop) -----------------------------------


async def test_user_prompt_submit_block_aborts_run(hook_module):
    hook_module.deny = lambda payload: {"decision": "deny", "reason": "prompt rejected"}
    hooks = build_hooks(
        [
            HookSpec(
                event="UserPromptSubmit",
                transport="python",
                entrypoint="vikram_test_hooks:deny",
            )
        ]
    )
    agent = HookedAgent(TestModel(), run_hooks=hooks)
    with pytest.raises(HookBlockedError, match="prompt rejected"):
        await agent.run("hello")


async def test_user_prompt_submit_injects_context(hook_module):
    seen = {}

    def capture(payload):
        seen["prompt"] = payload["prompt"]
        return {"additional_context": "REMEMBER: be terse"}

    hook_module.capture = capture
    hooks = build_hooks(
        [
            HookSpec(
                event="UserPromptSubmit",
                transport="python",
                entrypoint="vikram_test_hooks:capture",
            )
        ]
    )
    agent = HookedAgent(TestModel(), run_hooks=hooks)
    result = await agent.run("original question")

    # The hook sees the original prompt...
    assert seen["prompt"] == "original question"
    # ...and the injected context reached the model as part of the user message.
    user_texts = [
        part.content
        for msg in result.all_messages()
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", None) == "user-prompt"
    ]
    assert any("REMEMBER: be terse" in text for text in user_texts)
    assert any("original question" in text for text in user_texts)


async def test_stop_hook_fires_with_output(hook_module):
    captured = {}

    def on_stop(payload):
        captured["event"] = payload["event"]
        captured["output"] = payload["output"]

    hook_module.on_stop = on_stop
    hooks = build_hooks(
        [
            HookSpec(
                event="Stop", transport="python", entrypoint="vikram_test_hooks:on_stop"
            )
        ]
    )
    agent = HookedAgent(TestModel(custom_output_text="done"), run_hooks=hooks)
    await agent.run("go")
    assert captured["event"] == "Stop"
    assert captured["output"] == "done"


# --- build_agent wiring ------------------------------------------------------


def _spec_with_hooks(tmp_path, shared_dir, hooks):
    (tmp_path / "system_prompt.md").write_text("PROMPT", encoding="utf-8")
    return AgentSpec(
        name="Hooked",
        description="agent with hooks",
        system_prompt=tmp_path / "system_prompt.md",
        agent_dir=tmp_path,
        shared_dir=shared_dir,
        tools=["read_file"],
        hooks=hooks,
    )


def test_build_agent_wraps_tools_when_tool_hooks_present(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(
        tmp_path,
        settings.spec_root / "shared",
        [HookSpec(event="PreToolUse", command="true")],
    )
    agent = build_agent(spec=spec, settings=settings)
    assert any(isinstance(t, HookToolset) for t in agent.toolsets)


def test_build_agent_returns_hooked_agent_for_run_hooks(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(
        tmp_path,
        settings.spec_root / "shared",
        [HookSpec(event="Stop", command="true")],
    )
    agent = build_agent(spec=spec, settings=settings)
    assert isinstance(agent, HookedAgent)


def test_build_agent_plain_agent_without_hooks(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(tmp_path, settings.spec_root / "shared", [])
    agent = build_agent(spec=spec, settings=settings)
    assert type(agent) is Agent
    assert not any(isinstance(t, HookToolset) for t in agent.toolsets)


def test_shipped_specs_have_no_hooks_by_default(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    for name in ("vikram", "coder"):
        spec = load_spec(name, settings.spec_root)
        assert spec.hooks == []
