from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def test_install_script_delegates_model_config_to_cli():
    script = (APP_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "Vikram has no default model provider or model name." in script
    assert '"$bin_dir/vikram" configure' in script
    assert 'VIKRAM_MODEL="qwen3"' not in script
    assert "VIKRAM_MODEL:-qwen3" not in script
