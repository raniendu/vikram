from typing import get_args

from vikram.providers import PROVIDER_IDS, PROVIDERS, ModelProvider
from vikram.settings import VikramSettings


def test_registry_ids_match_model_provider_literal():
    assert set(PROVIDER_IDS) == set(get_args(ModelProvider))


def test_providers_needing_keys_declare_field_and_env():
    for entry in PROVIDERS.values():
        if entry.needs_api_key:
            assert entry.api_key_field, entry.id
            assert entry.api_key_env, entry.id


def test_registry_fields_exist_on_settings():
    fields = VikramSettings.model_fields
    for entry in PROVIDERS.values():
        if entry.api_key_field:
            assert entry.api_key_field in fields, entry.id
        if entry.base_url_field:
            assert entry.base_url_field in fields, entry.id


def test_every_provider_has_display_name_and_builder():
    for entry in PROVIDERS.values():
        assert entry.display_name, entry.id
        assert callable(entry.build), entry.id


def test_wizard_suggestions_cover_fixed_endpoint_providers():
    # The generic openai-compatible provider has no meaningful default model;
    # every concrete provider suggests one so the wizard can offer a default.
    for entry in PROVIDERS.values():
        if entry.id != "openai-compatible":
            assert entry.suggested_model, entry.id
