from __future__ import annotations

import pytest

from vikram.spec import (
    LOCAL_SURFACES,
    NETWORK_SURFACES,
    AgentSurfaceError,
    ensure_surface_allowed,
    load_spec,
)


@pytest.fixture
def coder_spec():
    from vikram.settings import VikramSettings

    return load_spec("coder", VikramSettings(_env_file=None).spec_root)


def test_coder_spec_is_still_marked_cli_only(coder_spec):
    assert coder_spec.cli_only is True


@pytest.mark.parametrize("surface", sorted(LOCAL_SURFACES))
def test_local_surfaces_may_run_a_local_only_agent(coder_spec, surface):
    ensure_surface_allowed(coder_spec, surface)


@pytest.mark.parametrize("surface", sorted(NETWORK_SURFACES))
def test_network_surfaces_may_not(coder_spec, surface):
    with pytest.raises(AgentSurfaceError, match="local-only"):
        ensure_surface_allowed(coder_spec, surface)


def test_gui_is_local_and_http_is_not():
    """The whole point of the split: the GUI runs coder, the HTTP API cannot."""
    assert "gui" in LOCAL_SURFACES
    assert "http" in NETWORK_SURFACES
    assert not (LOCAL_SURFACES & NETWORK_SURFACES)


def test_acp_is_local_which_matches_what_it_already_did():
    """acp.py builds cli_only agents directly, never calling the guard."""
    assert "acp" in LOCAL_SURFACES


def test_unknown_surface_is_treated_as_untrusted(coder_spec):
    with pytest.raises(AgentSurfaceError):
        ensure_surface_allowed(coder_spec, "some-future-webhook")
