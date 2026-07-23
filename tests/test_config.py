import stat
import tomllib

from vikram.config import (
    load_config,
    merge_write_config,
    migrate_v1,
)


def test_migrate_v1_ollama_flat_config():
    migrated = migrate_v1(
        {
            "model_provider": "ollama",
            "model": "llama3.2",
            "ollama_base_url": "http://localhost:11434",
        }
    )

    assert migrated == {
        "default_provider": "ollama",
        "providers": {
            "ollama": {
                "base_url": "http://localhost:11434",
                "model": "llama3.2",
            }
        },
    }


def test_migrate_v1_openai_compatible_with_env_style_keys():
    migrated = migrate_v1(
        {
            "model_provider": "openai-compatible",
            "model": "sarvam-m",
            "OPENAI_API_KEY": "sk-live",
            "VIKRAM_OPENAI_COMPAT_BASE_URL": "https://api.sarvam.ai/v1",
        }
    )

    assert migrated["default_provider"] == "openai-compatible"
    assert migrated["providers"]["openai-compatible"] == {
        "api_key": "sk-live",
        "base_url": "https://api.sarvam.ai/v1",
        "model": "sarvam-m",
    }


def test_migrate_v1_keeps_model_without_provider():
    assert migrate_v1({"model": "llama3.2"}) == {"model": "llama3.2"}


def test_migrate_v1_passes_v2_data_through_untouched():
    data = {"config_version": 2, "providers": {"ollama": {"model": "x"}}}

    assert migrate_v1(data) is data


def test_load_config_flattens_v2_layout(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "config_version = 2",
                'default_provider = "anthropic"',
                "",
                "[providers.anthropic]",
                'model = "claude-sonnet-5"',
                'api_key = "sk-ant"',
                "",
                "[providers.ollama]",
                'model = "llama3.2"',
                'base_url = "http://localhost:11434"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert load_config(config_file) == {
        "model_provider": "anthropic",
        "provider_models": {
            "anthropic": "claude-sonnet-5",
            "ollama": "llama3.2",
        },
        "anthropic_api_key": "sk-ant",
        "ollama_base_url": "http://localhost:11434",
    }


def test_load_config_still_reads_v1_flat_files(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                'model_provider = "ollama"',
                'model = "llama3.2"',
                'ollama_base_url = "http://localhost:11434"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert load_config(config_file) == {
        "model_provider": "ollama",
        "provider_models": {"ollama": "llama3.2"},
        "ollama_base_url": "http://localhost:11434",
    }


def test_load_config_returns_empty_on_corrupt_file(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("not [valid toml", encoding="utf-8")

    assert load_config(config_file) == {}


def test_merge_write_preserves_other_providers_and_unknown_keys(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "config_version = 2",
                'default_provider = "anthropic"',
                'custom_note = "keep me"',
                "",
                "[providers.anthropic]",
                'model = "claude-sonnet-5"',
                'api_key = "sk-ant"',
                "",
                "[future_section]",
                'flag = "yes"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    merge_write_config(
        {"providers": {"ollama": {"model": "llama3.2"}}},
        default_provider="ollama",
        path=config_file,
    )

    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert data["config_version"] == 2
    assert data["default_provider"] == "ollama"
    assert data["custom_note"] == "keep me"
    assert data["future_section"] == {"flag": "yes"}
    assert data["providers"]["anthropic"] == {
        "model": "claude-sonnet-5",
        "api_key": "sk-ant",
    }
    assert data["providers"]["ollama"] == {"model": "llama3.2"}
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_merge_write_updates_single_provider_key_in_place(tmp_path):
    config_file = tmp_path / "config.toml"
    merge_write_config(
        {"providers": {"gemini": {"model": "gemini-2.5-flash", "api_key": "old"}}},
        default_provider="gemini",
        path=config_file,
    )

    merge_write_config(
        {"providers": {"gemini": {"api_key": "new"}}},
        path=config_file,
    )

    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert data["default_provider"] == "gemini"
    assert data["providers"]["gemini"] == {
        "model": "gemini-2.5-flash",
        "api_key": "new",
    }


def test_merge_write_migrates_v1_file_to_v2(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "# Written by `vikram configure`.",
                'model_provider = "ollama"',
                'model = "llama3.2"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    merge_write_config(
        {"providers": {"anthropic": {"model": "claude-sonnet-5", "api_key": "k"}}},
        default_provider="anthropic",
        path=config_file,
    )

    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert data["config_version"] == 2
    assert data["default_provider"] == "anthropic"
    assert data["providers"]["ollama"] == {"model": "llama3.2"}
    assert data["providers"]["anthropic"] == {
        "model": "claude-sonnet-5",
        "api_key": "k",
    }
    assert "model_provider" not in data


def test_merge_write_backs_up_corrupt_file(tmp_path, capsys):
    config_file = tmp_path / "config.toml"
    config_file.write_text("not [valid toml", encoding="utf-8")

    merge_write_config(
        {"providers": {"ollama": {"model": "llama3.2"}}},
        default_provider="ollama",
        path=config_file,
    )

    backup = tmp_path / "config.toml.bak"
    assert backup.read_text(encoding="utf-8") == "not [valid toml"
    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert data["providers"]["ollama"] == {"model": "llama3.2"}
    assert "backed up" in capsys.readouterr().err


def test_emitted_file_round_trips_and_keeps_header(tmp_path):
    config_file = tmp_path / "config.toml"

    merge_write_config(
        {
            "providers": {
                "ollama-cloud": {"model": "gpt-oss:120b", "api_key": "cloud-key"}
            }
        },
        default_provider="ollama-cloud",
        path=config_file,
    )

    text = config_file.read_text(encoding="utf-8")
    assert text.startswith("# Written by `vikram configure`.")
    data = tomllib.loads(text)
    assert data == {
        "config_version": 2,
        "default_provider": "ollama-cloud",
        "providers": {
            "ollama-cloud": {"model": "gpt-oss:120b", "api_key": "cloud-key"}
        },
    }
