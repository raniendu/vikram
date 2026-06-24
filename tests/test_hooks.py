import sys
import types

import pytest

from vikram.agent import _strands_hook_callbacks, build_agent
from vikram.hooks import (
    HookBlockedError,
    HookConfigError,
    HookSpec,
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


# --- Strands tool hook callbacks (Pre/PostToolUse) ----------------------------


async def test_strands_pre_tool_hook_cancels_call(hook_module):
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
    (callback,) = _strands_hook_callbacks(hooks, "t")
    event = types.SimpleNamespace(
        tool_use={
            "toolUseId": "u1",
            "name": "run_command",
            "input": {"command": "rm -rf /"},
        },
        cancel_tool=None,
    )

    await callback(event)

    assert event.cancel_tool == "blocked by policy"


async def test_strands_post_tool_hook_appends_context(hook_module):
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
    (callback,) = _strands_hook_callbacks(hooks, "t")
    event = types.SimpleNamespace(
        tool_use={"toolUseId": "u1", "name": "read_file", "input": {"path": "x"}},
        result={
            "toolUseId": "u1",
            "status": "success",
            "content": [{"text": "tool output"}],
        },
    )

    await callback(event)

    assert event.result == {
        "toolUseId": "u1",
        "status": "success",
        "content": [{"text": "tool output"}, {"text": "audited"}],
    }


async def test_strands_post_tool_hook_returns_error_result(hook_module):
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
    (callback,) = _strands_hook_callbacks(hooks, "t")
    event = types.SimpleNamespace(
        tool_use={"toolUseId": "u1", "name": "read_file", "input": {"path": "x"}},
        result={
            "toolUseId": "u1",
            "status": "success",
            "content": [{"text": "tool output"}],
        },
    )

    await callback(event)

    assert event.result == {
        "toolUseId": "u1",
        "status": "error",
        "content": [{"text": "bad result"}],
    }


async def test_strands_pre_tool_hook_passthrough_leaves_call_uncancelled(hook_module):
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
    (callback,) = _strands_hook_callbacks(hooks, "t")
    event = types.SimpleNamespace(
        tool_use={"toolUseId": "u1", "name": "read_file", "input": {"path": "x"}},
        cancel_tool=None,
    )

    await callback(event)

    assert event.cancel_tool is None


# --- VikramAgent run hooks (UserPromptSubmit / Stop) --------------------------


async def test_user_prompt_submit_block_aborts_run(hook_module, monkeypatch, tmp_path):
    hook_module.deny = lambda payload: {"decision": "deny", "reason": "prompt rejected"}
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(
        tmp_path,
        settings.spec_root / "shared",
        [
            HookSpec(
                event="UserPromptSubmit",
                transport="python",
                entrypoint="vikram_test_hooks:deny",
            )
        ],
    )
    agent = build_agent(spec=spec, settings=settings)

    with pytest.raises(HookBlockedError, match="prompt rejected"):
        await agent._apply_user_prompt_hooks("hello")


async def test_user_prompt_submit_injects_context(hook_module, monkeypatch, tmp_path):
    seen = {}

    def capture(payload):
        seen["prompt"] = payload["prompt"]
        return {"additional_context": "REMEMBER: be terse"}

    hook_module.capture = capture
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(
        tmp_path,
        settings.spec_root / "shared",
        [
            HookSpec(
                event="UserPromptSubmit",
                transport="python",
                entrypoint="vikram_test_hooks:capture",
            )
        ],
    )
    agent = build_agent(spec=spec, settings=settings)
    prompt = await agent._apply_user_prompt_hooks("original question")

    # The hook sees the original prompt...
    assert seen["prompt"] == "original question"
    # ...and the injected context reached the model as part of the user message.
    assert prompt == "REMEMBER: be terse\n\noriginal question"


async def test_stop_hook_fires_with_output(hook_module, monkeypatch, tmp_path):
    captured = {}

    def on_stop(payload):
        captured["event"] = payload["event"]
        captured["output"] = payload["output"]

    hook_module.on_stop = on_stop
    hook_specs = [
        HookSpec(
            event="Stop", transport="python", entrypoint="vikram_test_hooks:on_stop"
        )
    ]
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(tmp_path, settings.spec_root / "shared", hook_specs)
    agent = build_agent(spec=spec, settings=settings)

    class Result:
        context_size = 0

        def __str__(self):
            return "done"

    class FakeRawAgent:
        messages = [{"role": "assistant", "content": "done"}]

        async def invoke_async(self, prompt, *, invocation_state):
            return Result()

    fake_raw_agent = FakeRawAgent()
    monkeypatch.setattr(agent, "_agent_for_run", lambda: fake_raw_agent)

    result = await agent.run("go")

    assert result.output == "done"
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


def test_build_agent_registers_strands_tool_hooks(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(
        tmp_path,
        settings.spec_root / "shared",
        [HookSpec(event="PreToolUse", command="true")],
    )
    agent = build_agent(spec=spec, settings=settings)

    assert agent._hookset.has_tool_hooks
    assert len(agent._agent_kwargs["hooks"]) == 1


def test_build_agent_records_run_hooks(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(
        tmp_path,
        settings.spec_root / "shared",
        [HookSpec(event="Stop", command="true")],
    )
    agent = build_agent(spec=spec, settings=settings)

    assert agent.runtime == "strands"
    assert agent._hookset.has_run_hooks


def test_build_agent_plain_agent_without_hooks(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    spec = _spec_with_hooks(tmp_path, settings.spec_root / "shared", [])
    agent = build_agent(spec=spec, settings=settings)

    assert agent.runtime == "strands"
    assert agent._agent_kwargs["hooks"] == []
    assert not agent._hookset.has_tool_hooks
    assert not agent._hookset.has_run_hooks


def test_shipped_specs_have_no_hooks_by_default(monkeypatch, tmp_path):
    settings = _local_model_settings(monkeypatch, tmp_path)
    for name in ("vikram", "coder"):
        spec = load_spec(name, settings.spec_root)
        assert spec.hooks == []
