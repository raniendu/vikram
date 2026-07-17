import json
from pathlib import Path

from vikram.doctor import collect_diagnostics, run

APP_ROOT = Path(__file__).resolve().parents[1]


def _clean_environment(monkeypatch, tmp_path):
    for name in (
        "VIKRAM_MODEL",
        "VIKRAM_MODEL_PROVIDER",
        "VIKRAM_OPENAI_COMPAT_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VIKRAM_SPEC_ROOT", str(APP_ROOT / "spec"))


def test_doctor_accepts_agent_model_defaults(monkeypatch, tmp_path):
    _clean_environment(monkeypatch, tmp_path)

    diagnostics = collect_diagnostics(
        agent_name="coder",
        cwd=APP_ROOT,
        config_file=tmp_path / "missing.toml",
    )

    by_name = {item.name: item for item in diagnostics}
    assert by_name["Agent spec"].status == "ok"
    assert by_name["Model provider"].detail == "ollama"
    assert by_name["Model"].detail == "qwen3.6:35b-mlx"
    assert by_name["Command policy"].status == "ok"
    assert not [item for item in diagnostics if item.status == "error"]


def test_doctor_reports_missing_default_model(monkeypatch, tmp_path):
    _clean_environment(monkeypatch, tmp_path)

    diagnostics = collect_diagnostics(
        agent_name="vikram",
        cwd=APP_ROOT,
        config_file=tmp_path / "missing.toml",
    )

    errors = {item.name: item for item in diagnostics if item.status == "error"}
    assert errors["Model provider"].fix == (
        "Run `vikram configure` or select a configured agent."
    )
    assert errors["Model"].fix == "Run `vikram configure` or set VIKRAM_MODEL."


def test_doctor_json_never_prints_api_key(monkeypatch, tmp_path, capsys):
    _clean_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("VIKRAM_MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("VIKRAM_MODEL", "test-model")
    monkeypatch.setenv("VIKRAM_OPENAI_COMPAT_API_KEY", "super-secret-value")

    result = run(["--json", "--agent", "vikram"])

    assert result == 0
    output = capsys.readouterr().out
    assert "super-secret-value" not in output
    diagnostics = json.loads(output)["diagnostics"]
    assert (
        next(item for item in diagnostics if item["name"] == "API credential")["detail"]
        == "available"
    )
