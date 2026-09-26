"""Before/after quality evals for Vikram agents.

Unit tests in ``tests/`` prove the code works; this package measures how well
the agents answer. A post-commit hook runs the suite whenever a commit changes
something that can move answer quality (prompts, models, model settings, tools,
skills, MCP servers, hooks, framework versions) and commits a metrics-only
record to ``evals/history/``. See ``docs/evals.md``.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_VERSION = 1

SUITE_DIR = Path(__file__).resolve().parent
CASES_DIR = SUITE_DIR / "cases"
FIXTURES_DIR = SUITE_DIR / "fixtures"

# Relative to the repository root, not the suite directory: the suite can run
# from a copy outside the repo (baseline runs against older commits).
HISTORY_RELPATH = Path("evals") / "history"
STATE_RELPATH = Path(".vikram") / "evals"
