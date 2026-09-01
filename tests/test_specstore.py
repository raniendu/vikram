from __future__ import annotations

import pytest

from vikram.settings import VikramSettings
from vikram.spec import AgentSpecDraft
from vikram.specstore import (
    AgentNotFoundError,
    AgentReadOnlyError,
    AgentStoreError,
    create_agent,
    delete_agent,
    duplicate_agent,
    get_agent,
    list_agents,
    load_agent,
    update_agent,
    user_agents_root,
    validate_agent_id,
)


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """Real shipped specs, but a throwaway config home for the user root."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("VIKRAM_MODEL", "VIKRAM_MODEL_PROVIDER", "VIKRAM_SPEC_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return VikramSettings(_env_file=None)


def _draft(**overrides) -> AgentSpecDraft:
    base = {
        "name": "Helper",
        "description": "A test agent.",
        "system_prompt": "system_prompt.md",
        "tools": ["read_file"],
    }
    return AgentSpecDraft(**{**base, **overrides})


# --- ids ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["..", "../escape", "a/b", "shared", "", "Upper", ".hidden", "x" * 65, "-lead"],
)
def test_rejects_ids_that_escape_or_collide(bad_id):
    with pytest.raises(AgentStoreError):
        validate_agent_id(bad_id)


@pytest.mark.parametrize("good_id", ["coder", "my-agent", "agent_2", "a"])
def test_accepts_ordinary_ids(good_id):
    validate_agent_id(good_id)


# --- roots and overlay -------------------------------------------------


def test_user_root_is_under_config_not_the_update_managed_checkout(settings):
    """~/.local/share/vikram is a git checkout that `vikram update` moves."""
    root = user_agents_root()

    assert root.name == "agents"
    assert ".local/share/vikram" not in str(root)


def test_lists_shipped_agents(settings):
    ids = {a.id for a in list_agents(settings)}

    assert {"coder", "vikram"} <= ids
    assert all(a.root == "builtin" and not a.writable for a in list_agents(settings))


def test_shared_dir_of_a_user_agent_points_at_the_shipped_shared_root(settings):
    create_agent("helper", _draft(), settings=settings, system_prompt="Be helpful.")

    spec = load_agent("helper", settings)

    assert spec.agent_dir == user_agents_root() / "helper"
    assert spec.shared_dir == settings.spec_root / "shared"
    assert spec.load_command_policy() is not None


def test_user_agent_shadows_a_builtin_of_the_same_id(settings):
    create_agent(
        "coder-x", _draft(name="Original"), settings=settings, system_prompt="p"
    )
    update_agent("coder", _draft(name="Shadowed"), settings=settings)

    summaries = {a.id: a for a in list_agents(settings)}

    assert summaries["coder"].root == "user"
    assert summaries["coder"].name == "Shadowed"
    assert summaries["coder"].shadows == "builtin"


def test_editing_a_builtin_never_writes_to_the_shipped_file(settings):
    shipped = settings.spec_root / "coder" / "agent.toml"
    before = shipped.read_text()

    update_agent("coder", _draft(name="Copied"), settings=settings)

    assert shipped.read_text() == before
    assert (user_agents_root() / "coder" / "agent.toml").is_file()


def test_deleting_the_user_copy_reveals_the_builtin_again(settings):
    update_agent("coder", _draft(name="Copied"), settings=settings)
    assert get_agent("coder", settings).summary.root == "user"

    delete_agent("coder", settings)

    assert get_agent("coder", settings).summary.root == "builtin"


# --- tolerance ---------------------------------------------------------


def test_a_malformed_spec_is_listed_as_an_error_rather_than_raising(settings):
    broken = user_agents_root() / "broken"
    broken.mkdir(parents=True)
    (broken / "agent.toml").write_text("not [valid toml")

    summaries = {a.id: a for a in list_agents(settings)}

    assert summaries["broken"].error is not None
    assert summaries["coder"].error is None


def test_a_malformed_spec_does_not_break_other_agents_builds(settings):
    """The regression this guards: one bad spec breaking every agent."""
    from vikram.delegation import discover_subagents

    broken = user_agents_root() / "broken"
    broken.mkdir(parents=True)
    (broken / "agent.toml").write_text("name = ")

    subagents = discover_subagents(settings, orchestrator_name="vikram", surface="cli")

    assert "broken" not in {s.name for s in subagents}
    assert "coder" in {s.name for s in subagents}


def test_user_agents_are_delegable(settings):
    from vikram.delegation import discover_subagents

    create_agent("helper", _draft(), settings=settings, system_prompt="p")

    subagents = {
        s.name: s
        for s in discover_subagents(settings, orchestrator_name="vikram", surface="cli")
    }

    assert subagents["helper"].available is True


def test_coder_is_available_to_delegate_on_local_surfaces(settings):
    from vikram.delegation import discover_subagents

    for surface in ("cli", "acp", "gui"):
        subagents = {
            s.name: s
            for s in discover_subagents(
                settings, orchestrator_name="vikram", surface=surface
            )
        }
        assert subagents["coder"].available is True, surface


def test_coder_stays_unavailable_to_delegate_on_network_surfaces(settings):
    from vikram.delegation import discover_subagents

    subagents = {
        s.name: s
        for s in discover_subagents(
            settings, orchestrator_name="vikram", surface="http"
        )
    }

    assert subagents["coder"].available is False
    assert "local-only" in subagents["coder"].unavailable_reason


# --- CRUD --------------------------------------------------------------


def test_create_writes_spec_and_prompt(settings):
    detail = create_agent(
        "helper", _draft(), settings=settings, system_prompt="Be helpful."
    )

    assert detail.summary.root == "user"
    assert detail.summary.writable is True
    assert detail.system_prompt == "Be helpful."
    assert (user_agents_root() / "helper" / "system_prompt.md").is_file()


def test_create_rejects_an_existing_id(settings):
    create_agent("helper", _draft(), settings=settings, system_prompt="p")

    with pytest.raises(AgentStoreError, match="already exists"):
        create_agent("helper", _draft(), settings=settings, system_prompt="p")


def test_create_rejects_shadowing_a_builtin_by_accident(settings):
    with pytest.raises(AgentStoreError, match="already exists"):
        create_agent("coder", _draft(), settings=settings, system_prompt="p")


def test_update_preserves_comments_a_user_wrote(settings):
    create_agent("helper", _draft(), settings=settings, system_prompt="p")
    path = user_agents_root() / "helper" / "agent.toml"
    path.write_text("# my note\n" + path.read_text())

    update_agent("helper", _draft(name="Renamed"), settings=settings)

    assert "# my note" in path.read_text()
    assert get_agent("helper", settings).summary.name == "Renamed"


def test_duplicate_copies_the_whole_agent_directory(settings):
    detail = duplicate_agent("coder", "coder-copy", settings=settings, name="My Coder")

    assert detail.summary.root == "user"
    assert detail.summary.name == "My Coder"
    assert (user_agents_root() / "coder-copy" / "system_prompt.md").is_file()
    assert detail.draft.tools == get_agent("coder", settings).draft.tools


def test_delete_refuses_a_builtin(settings):
    with pytest.raises(AgentReadOnlyError, match="built-in"):
        delete_agent("coder", settings)


def test_missing_agent_raises_not_found(settings):
    with pytest.raises(AgentNotFoundError):
        get_agent("nope", settings)
    with pytest.raises(AgentNotFoundError):
        load_agent("nope", settings)


def test_get_agent_exposes_raw_toml_for_the_escape_hatch(settings):
    detail = get_agent("vikram", settings)

    assert "[[mcp_servers]]" in detail.source_toml
    assert detail.draft.name == "Vikram"


def test_summary_reports_resolved_model_not_just_the_spec_pin(settings):
    summary = {a.id: a for a in list_agents(settings)}["coder"]

    assert summary.spec_provider == "ollama"
    assert summary.resolved_provider == "ollama"
    assert summary.resolved_model == summary.spec_model


def test_build_agent_finds_a_user_created_agent(settings, monkeypatch):
    """The Phase 1 payoff: `vikram --agent helper` works before any GUI."""
    from vikram.agent import build_agent

    create_agent(
        "helper",
        _draft(model_provider="ollama", model="test-model"),
        settings=settings,
        system_prompt="Be helpful.",
    )

    agent = build_agent(
        settings=settings.model_copy(update={"default_agent": "helper"})
    )

    assert agent.name == "Helper"
    assert "read_file" in agent.tool_names


def test_agent_not_found_is_a_file_not_found_error():
    """api.py and gateway.py map FileNotFoundError to 404; keep that working."""
    assert issubclass(AgentNotFoundError, FileNotFoundError)


@pytest.mark.parametrize("bad_id", ["..", "../escape", "a/b", "shared", "", "."])
def test_reads_reject_traversal(bad_id, settings):
    from vikram.specstore import ensure_safe_agent_id

    with pytest.raises(AgentStoreError):
        ensure_safe_agent_id(bad_id)


@pytest.mark.parametrize("legacy_id", ["MyAgent", "agent.v2"])
def test_reads_tolerate_ids_that_predate_the_naming_rule(legacy_id):
    """A spec dir named before the convention existed must still load."""
    from vikram.specstore import ensure_safe_agent_id

    ensure_safe_agent_id(legacy_id)
    with pytest.raises(AgentStoreError):
        validate_agent_id(legacy_id)


def test_doctor_finds_a_user_created_agent(settings):
    from vikram.doctor import collect_diagnostics

    create_agent("helper", _draft(), settings=settings, system_prompt="p")

    by_name = {d.name: d for d in collect_diagnostics(agent_name="helper")}

    assert by_name["Agent spec"].status == "ok"
    assert "helper" in by_name["Agent spec"].detail
