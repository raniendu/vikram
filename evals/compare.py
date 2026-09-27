"""Before/after comparison of two eval results, as a plain-text table."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals import STATE_RELPATH, history


class ResultNotFound(LookupError):
    pass


def resolve_result(repo: Path, ref: str, agent: str | None = None) -> dict[str, Any]:
    """Find a result by file path, run id, manual label, or commit sha prefix."""
    path = Path(ref)
    if path.suffix == ".json" and path.is_file():
        return json.loads(path.read_text())
    manual = repo / STATE_RELPATH / "runs" / f"manual-{ref}" / "result.json"
    if manual.is_file():
        return json.loads(manual.read_text())
    records = history.load_records(repo, agent)
    for record in reversed(records):
        if record["run_id"] == ref:
            return record
    matches = [r for r in records if r["commit"]["sha"].startswith(ref)]
    if matches:
        agents = {r["agent"] for r in matches}
        if len(agents) > 1:
            raise ResultNotFound(
                f"{ref!r} has results for several agents ({', '.join(sorted(agents))}); "
                "pass --agent."
            )
        return matches[-1]
    raise ResultNotFound(f"No eval result matches {ref!r}.")


def _fmt(value: Any, kind: str) -> str:
    if value is None:
        return "-"
    if kind == "rate":
        return f"{value * 100:.0f}%"
    if kind == "score":
        return f"{value:.2f}"
    if kind == "ms":
        return f"{value / 1000:.1f}s"
    if kind == "int":
        return f"{value:.0f}"
    return f"{value:.1f}"


def _signed(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "rate":
        return f"{value * 100:+.0f}pt"
    if kind == "score":
        return f"{value:+.2f}"
    if kind == "pct":
        return f"{value:+.0f}%"
    return f"{value:+.1f}"


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def render(before: dict[str, Any], after: dict[str, Any]) -> str:
    if before.get("suite_hash") != after.get("suite_hash"):
        warning = (
            "WARNING: the two runs used different eval suites; "
            "scores are not directly comparable.\n\n"
        )
    else:
        warning = ""
    delta = history.compute_delta(after, before)
    b, a, d = before["summary"], after["summary"], delta["summary"]

    def side(result: dict[str, Any]) -> str:
        commit = result.get("commit", {}).get("sha", "")[:7] or "manual"
        env = result.get("env", {})
        return f"{commit} ({env.get('model') or '?'})"

    header = f"{after['agent']}: {side(before)} -> {side(after)}\n"
    change = after.get("change") or {}
    if change.get("kinds"):
        header += f"change: {', '.join(change['kinds'])}\n"
        for key, value in (change.get("details") or {}).items():
            header += f"  {key}: {json.dumps(value)}\n"

    summary_rows = [
        ["metric", "before", "after", "change"],
        [
            "pass rate",
            _fmt(b.get("pass_rate"), "rate"),
            _fmt(a.get("pass_rate"), "rate"),
            _signed(d["pass_rate"], "rate"),
        ],
        [
            "judge score",
            _fmt(b.get("judge_mean"), "score"),
            _fmt(a.get("judge_mean"), "score"),
            _signed(d["judge_mean"], "score"),
        ],
        [
            "tokens / case",
            _fmt(b.get("tokens_mean"), "int"),
            _fmt(a.get("tokens_mean"), "int"),
            _signed(d["tokens_mean_pct"], "pct"),
        ],
        [
            "latency p50",
            _fmt(b.get("latency_p50_ms"), "ms"),
            _fmt(a.get("latency_p50_ms"), "ms"),
            _signed(d["latency_p50_pct"], "pct"),
        ],
        [
            "tool calls / case",
            _fmt(b.get("tool_calls_mean"), "num"),
            _fmt(a.get("tool_calls_mean"), "num"),
            _signed(d["tool_calls_mean"], "num"),
        ],
    ]

    order = {"worse": 0, "mixed": 1, "better": 2, "same": 3}
    before_cases = {c["id"]: c for c in before["cases"]}
    case_rows = [["case", "status", "pass before", "pass after", "judge", "tokens"]]
    for case_id, cd in sorted(
        delta["cases"].items(), key=lambda kv: (order[kv[1]["status"]], kv[0])
    ):
        after_case = next(c for c in after["cases"] if c["id"] == case_id)
        case_rows.append(
            [
                case_id,
                cd["status"].upper() if cd["status"] == "worse" else cd["status"],
                _fmt(before_cases[case_id].get("pass_rate"), "rate"),
                _fmt(after_case.get("pass_rate"), "rate"),
                _signed(cd["judge_mean"], "score"),
                _signed(cd["tokens_mean_pct"], "pct"),
            ]
        )
    skipped = [c["id"] for c in after["cases"] if c.get("status") == "skipped"]
    footer = f"\nskipped: {', '.join(skipped)}\n" if skipped else ""
    unscored = [c["id"] for c in after["cases"] if c.get("status") == "unscored"]
    if unscored:
        footer += f"unscored (model server busy): {', '.join(unscored)}\n"
    return (
        warning
        + header
        + "\n"
        + _table(summary_rows)
        + "\n\n"
        + _table(case_rows)
        + "\n"
        + footer
    )
