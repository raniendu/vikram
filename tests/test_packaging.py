from __future__ import annotations

import tomllib
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def test_wheel_includes_agent_specs() -> None:
    pyproject = tomllib.loads((APP_ROOT / "pyproject.toml").read_text())

    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]

    assert force_include["spec"] == "spec"
