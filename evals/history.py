"""Committed eval history: one metrics-only JSON record per agent per run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals import HISTORY_RELPATH, SCHEMA_VERSION, gitutil

# Case-level thresholds for calling a change "worse" or "better". With three
# repeats one flipped repeat moves pass_rate by 0.33, so smaller moves on a
# single case are treated as noise.
PASS_RATE_NOISE = 0.34
JUDGE_NOISE = 0.15

TRIGGERS = ("commit", "baseline", "model_check", "manual")


def history_dir(repo: Path) -> Path:
    return repo / HISTORY_RELPATH


def make_run_id(when: datetime, sha: str, agent: str) -> str:
    return f"{when.strftime('%Y-%m-%dT%H-%M-%S')}_{sha[:7]}_{agent}"


def record_path(repo: Path, record: dict[str, Any]) -> Path:
    when = datetime.fromisoformat(record["timestamp"])
    return history_dir(repo) / f"{when:%Y}" / f"{when:%m}" / f"{record['run_id']}.json"


def write_record(repo: Path, record: dict[str, Any]) -> Path:
    path = record_path(repo, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n")
    return path


def load_records(repo: Path, agent: str | None = None) -> list[dict[str, Any]]:
    records = []
    for path in sorted(history_dir(repo).rglob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("schema_version") != SCHEMA_VERSION:
            continue
        if agent is None or record.get("agent") == agent:
            records.append(record)
    records.sort(key=lambda r: r["timestamp"])
    return records


def find_baseline(
    repo: Path,
    records: list[dict[str, Any]],
    *,
    agent: str,
    head: str,
    suite_hash: str,
    include_head: bool = False,
) -> dict[str, Any] | None:
    """The record for ``agent`` on the nearest strict ancestor of ``head`` that
    ran the same suite; the newest run wins on that commit. Records from
    other branches are ignored. ``include_head`` also accepts a record on
    ``head`` itself (a working-tree run compares against its own commit)."""
    best: tuple[int, str, dict[str, Any]] | None = None
    for record in records:
        if record.get("agent") != agent or record.get("suite_hash") != suite_hash:
            continue
        sha = record["commit"]["sha"]
        if (sha == head and not include_head) or not gitutil.is_ancestor(
            repo, sha, head
        ):
            continue
        key = (gitutil.distance(repo, sha, head), record["timestamp"])
        if (
            best is None
            or key[0] < best[0]
            or (key[0] == best[0] and key[1] >= best[1])
        ):
            best = (*key, record)
    return best[2] if best else None


def latest_record(records: list[dict[str, Any]], agent: str) -> dict[str, Any] | None:
    for record in reversed(records):
        if record.get("agent") == agent:
            return record
    return None


# --- deltas ---------------------------------------------------------------


def _diff(after: Any, before: Any, *, digits: int = 4) -> float | None:
    if after is None or before is None:
        return None
    return round(after - before, digits)


def _pct(after: Any, before: Any) -> float | None:
    if after is None or before in (None, 0):
        return None
    return round((after - before) / before * 100, 1)


def case_status(delta_pass: float | None, delta_judge: float | None) -> str:
    worse = (delta_pass is not None and delta_pass <= -PASS_RATE_NOISE) or (
        delta_judge is not None and delta_judge <= -JUDGE_NOISE
    )
    better = (delta_pass is not None and delta_pass >= PASS_RATE_NOISE) or (
        delta_judge is not None and delta_judge >= JUDGE_NOISE
    )
    if worse and not better:
        return "worse"
    if better and not worse:
        return "better"
    if worse and better:
        return "mixed"
    return "same"


def compute_delta(after: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """Summary and per-case differences; ``after`` minus ``before``."""
    a, b = after["summary"], before["summary"]
    summary = {
        "pass_rate": _diff(a.get("pass_rate"), b.get("pass_rate")),
        "judge_mean": _diff(a.get("judge_mean"), b.get("judge_mean")),
        "tokens_mean_pct": _pct(a.get("tokens_mean"), b.get("tokens_mean")),
        "latency_p50_pct": _pct(a.get("latency_p50_ms"), b.get("latency_p50_ms")),
        "tool_calls_mean": _diff(a.get("tool_calls_mean"), b.get("tool_calls_mean")),
    }
    before_cases = {c["id"]: c for c in before["cases"] if c.get("status") == "ok"}
    cases: dict[str, dict[str, Any]] = {}
    for case in after["cases"]:
        prior = before_cases.get(case["id"])
        if case.get("status") != "ok" or prior is None:
            continue
        d_pass = _diff(case.get("pass_rate"), prior.get("pass_rate"))
        d_judge = _diff(case.get("judge_mean"), prior.get("judge_mean"))
        cases[case["id"]] = {
            "pass_rate": d_pass,
            "judge_mean": d_judge,
            "tokens_mean_pct": _pct(case.get("tokens_mean"), prior.get("tokens_mean")),
            "status": case_status(d_pass, d_judge),
        }
    summary["worse_cases"] = sorted(
        k for k, v in cases.items() if v["status"] == "worse"
    )
    summary["better_cases"] = sorted(
        k for k, v in cases.items() if v["status"] == "better"
    )
    return {"summary": summary, "cases": cases}


# --- records --------------------------------------------------------------


def build_record(
    *,
    repo: Path,
    result: dict[str, Any],
    sha: str,
    trigger: str,
    change: dict[str, Any],
    baseline: dict[str, Any] | None,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the committed record. Only metrics: no prompts or outputs."""
    when = when or datetime.now(timezone.utc)
    agent = result["agent"]
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": make_run_id(when, sha, agent),
        "timestamp": when.isoformat(timespec="microseconds"),
        "agent": agent,
        "trigger": trigger,
        "commit": {
            "sha": sha,
            "parent": gitutil.parent_of(repo, sha),
            "subject": gitutil.subject(repo, sha),
            "committed_at": gitutil.commit_time(repo, sha),
        },
        "change": change,
        "suite_hash": result["suite_hash"],
        "repeats": result["repeats"],
        "env": result["env"],
        "baseline_run_id": baseline["run_id"] if baseline else None,
        "summary": result["summary"],
        "delta": compute_delta(result, baseline) if baseline else None,
        "cases": result["cases"],
    }
    return record


def one_line_summary(record: dict[str, Any]) -> str:
    s = record["summary"]
    delta = record.get("delta")
    parts = [f"{record['agent']} @ {record['commit']['sha'][:7]}"]

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.0f}%"

    if delta is None:
        parts.append(f"pass {pct(s.get('pass_rate'))} (no baseline)")
    else:
        d = delta["summary"]
        before_pass = (
            None
            if s.get("pass_rate") is None or d.get("pass_rate") is None
            else s["pass_rate"] - d["pass_rate"]
        )
        arrow = ""
        if d.get("pass_rate"):
            arrow = " ▲" if d["pass_rate"] > 0 else " ▼"
        parts.append(f"pass {pct(before_pass)}→{pct(s.get('pass_rate'))}{arrow}")
        if d.get("judge_mean") is not None:
            parts.append(f"judge {d['judge_mean']:+.2f}")
        if d.get("tokens_mean_pct") is not None:
            parts.append(f"tokens {d['tokens_mean_pct']:+.0f}%")
        if d.get("worse_cases"):
            parts.append(f"worse: {', '.join(d['worse_cases'])}")
    kinds = record["change"].get("kinds") or []
    if kinds:
        parts.append(f"[{', '.join(kinds)}]")
    return " | ".join(parts)
