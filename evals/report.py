"""Static HTML trend report built from ``evals/history``."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

from evals import history

# Categorical slots 1 and 2 of the validated reference palette (light, dark).
_SERIES = {
    "pass_rate": ("Pass rate", "#2a78d6", "#3987e5"),
    "judge_mean": ("Judge score", "#eb6834", "#d95926"),
}

_W, _H = 760, 240
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 44, 16, 16, 34


def _pct(value: Any) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _num(value: Any, spec: str, empty: str = "–") -> str:
    return empty if value is None else format(value, spec)


def _points(value: Any) -> str:
    """Signed change in a 0..1 rate, as percentage points."""
    return "" if value is None else f"{value * 100:+.0f}pt"


def _signed(value: Any, spec: str = "+.2f", unit: str = "") -> str:
    return "" if value is None else f"{format(value, spec)}{unit}"


def _chart(records: list[dict[str, Any]]) -> str:
    n = len(records)
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B

    def x(i: int) -> float:
        return _PAD_L + (plot_w / 2 if n == 1 else plot_w * i / (n - 1))

    def y(v: float) -> float:
        return _PAD_T + plot_h * (1 - v)

    parts = [
        f'<svg viewBox="0 0 {_W} {_H}" role="img" '
        'aria-label="Pass rate and judge score per run, 0 to 100 percent">'
    ]
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(
            f'<line class="grid" x1="{_PAD_L}" x2="{_W - _PAD_R}" '
            f'y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
            f'<text class="tick" x="{_PAD_L - 8}" y="{y(tick) + 4:.1f}" '
            f'text-anchor="end">{tick * 100:.0f}%</text>'
        )
    step = max(1, n // 8)
    for i, record in enumerate(records):
        if i % step == 0 or i == n - 1:
            # Edge labels align inward so they are never clipped.
            anchor = "middle" if n == 1 else "start" if i == 0 else "middle"
            if n > 1 and i == n - 1:
                anchor = "end"
            parts.append(
                f'<text class="tick" x="{x(i):.1f}" y="{_H - 12}" '
                f'text-anchor="{anchor}">{escape(record["commit"]["sha"][:7])}</text>'
            )
    for key, (label, _, _) in _SERIES.items():
        points = [(i, r["summary"].get(key)) for i, r in enumerate(records)]
        points = [(i, v) for i, v in points if v is not None]
        if not points:
            continue
        path = " ".join(
            f"{'M' if j == 0 else 'L'}{x(i):.1f},{y(v):.1f}"
            for j, (i, v) in enumerate(points)
        )
        parts.append(f'<path class="line s-{key}" d="{path}"/>')
        for i, v in points:
            record = records[i]
            kinds = ", ".join(record["change"].get("kinds") or []) or record["trigger"]
            tip = (
                f"{label}: {v * 100:.0f}%\n{record['commit']['sha'][:7]} "
                f"{record['commit']['subject']}\n{kinds}"
            )
            parts.append(
                f'<g class="pt"><circle class="hit" cx="{x(i):.1f}" cy="{y(v):.1f}" '
                f'r="10"/><circle class="dot s-{key}" cx="{x(i):.1f}" '
                f'cy="{y(v):.1f}" r="4.5"/><title>{escape(tip)}</title></g>'
            )
        last_i, last_v = points[-1]
        parts.append(
            f'<text class="direct s-{key}-t" x="{min(x(last_i) + 8, _W - 4):.1f}" '
            f'y="{y(last_v) - 8:.1f}" text-anchor="end">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _change_cell(record: dict[str, Any]) -> str:
    change = record["change"]
    kinds = change.get("kinds") or []
    chips = "".join(f'<span class="chip">{escape(k)}</span>' for k in kinds)
    if not kinds:
        chips = f'<span class="muted">{escape(record["trigger"])}</span>'
    details = change.get("details") or {}
    lines = "".join(
        f"<div><code>{escape(k)}</code> {escape(json.dumps(v))}</div>"
        for k, v in details.items()
    )
    return chips + (f'<div class="det">{lines}</div>' if lines else "")


def _case_table(record: dict[str, Any]) -> str:
    deltas = (record.get("delta") or {}).get("cases", {})
    rows = []
    for case in record["cases"]:
        if case.get("status") != "ok":
            rows.append(
                f"<tr><td><code>{escape(case['id'])}</code></td>"
                f'<td colspan="5" class="muted">skipped ({escape(case.get("reason", ""))})'
                "</td></tr>"
            )
            continue
        d = deltas.get(case["id"], {})
        status = d.get("status", "")
        failed = ", ".join(f"{k}×{v}" for k, v in case.get("failed_checks", {}).items())
        rows.append(
            f'<tr class="st-{status}"><td><code>{escape(case["id"])}</code></td>'
            f"<td>{_pct(case['pass_rate'])} {_points(d.get('pass_rate'))}</td>"
            f"<td>{_num(case.get('judge_mean'), '.2f', '')}</td>"
            f"<td>{_num(case.get('tokens_mean'), '.0f', '')}</td>"
            f"<td>{escape(status)}</td><td class='muted'>{escape(failed)}</td></tr>"
        )
    return (
        '<div class="scroll"><table class="cases"><thead><tr><th>Case</th><th>Pass</th>'
        "<th>Judge</th><th>Tokens</th><th>vs baseline</th><th>Failed checks</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _runs_table(records: list[dict[str, Any]]) -> str:
    rows = []
    for record in reversed(records):
        s = record["summary"]
        d = (record.get("delta") or {}).get("summary", {})
        worse = ", ".join(d.get("worse_cases", []))
        rows.append(
            "<tr>"
            f"<td>{escape(record['timestamp'][:16].replace('T', ' '))}</td>"
            f"<td><code>{escape(record['commit']['sha'][:7])}</code> "
            f"{escape(record['commit']['subject'][:60])}</td>"
            f"<td>{_change_cell(record)}</td>"
            f"<td>{_pct(s.get('pass_rate'))} "
            f"<span class='d'>{_points(d.get('pass_rate'))}</span></td>"
            f"<td>{_num(s.get('judge_mean'), '.2f')} "
            f"<span class='d'>{_signed(d.get('judge_mean'))}</span></td>"
            f"<td>{_signed(d.get('tokens_mean_pct'), '+.0f', '%')}</td>"
            f"<td>{escape(str(record['env'].get('model') or ''))}</td>"
            f"<td class='worse'>{escape(worse)}</td>"
            "</tr>"
            f'<tr class="more"><td colspan="8"><details><summary>Cases</summary>'
            f"{_case_table(record)}</details></td></tr>"
        )
    return (
        '<div class="scroll"><table class="runs"><thead><tr><th>When (UTC)</th>'
        "<th>Commit</th><th>Change</th><th>Pass</th><th>Judge</th><th>Tokens</th>"
        "<th>Model</th><th>Worse cases</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


_CSS = """
:root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:#e4e3de;--chip:#eef3fb;
--s1:%s;--s2:%s;--bad:#d03b3b;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--line:#383835;--chip:#23324a;--s1:%s;--s2:%s;--bad:#e66767;color-scheme:dark}}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;
margin:0;padding:24px 16px}
main{max-width:1100px;margin:0 auto;display:flex;flex-direction:column;gap:40px}
h1{margin:0;font-size:28px}h2{margin:0 0 8px;font-size:21px}
.muted,.tick{color:var(--ink2);fill:var(--ink2)}.tick{font-size:11px}
svg{width:100%%;min-width:520px;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}.line{fill:none;stroke-width:2}
.s-pass_rate{stroke:var(--s1);fill:var(--s1)}.s-judge_mean{stroke:var(--s2);fill:var(--s2)}
.line.s-pass_rate,.line.s-judge_mean{fill:none}
.dot{stroke:var(--bg);stroke-width:2}.hit{fill:transparent}
.pt:hover .dot{r:6}.direct{font-size:12px;fill:var(--ink2)}
.legend{display:flex;gap:16px;font-size:13px;color:var(--ink2)}
.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;
vertical-align:middle;margin-right:6px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%%;font-size:14px}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--ink2);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.runs{min-width:900px}.d{color:var(--ink2);font-size:13px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:0 8px;
margin:0 4px 2px 0;font-size:12px}
.det{font-size:12px;color:var(--ink2);margin-top:4px}.worse,.st-worse td{color:var(--bad)}
tr.more td{border-bottom:1px solid var(--line);padding-top:0}
details summary{cursor:pointer;color:var(--ink2);font-size:13px}
code{font-size:12.5px}
""" % (
    _SERIES["pass_rate"][1],
    _SERIES["judge_mean"][1],
    _SERIES["pass_rate"][2],
    _SERIES["judge_mean"][2],
)


def render(records: list[dict[str, Any]]) -> str:
    agents = sorted({r["agent"] for r in records})
    sections = []
    for agent in agents:
        runs = [r for r in records if r["agent"] == agent]
        legend = "".join(
            f'<span><i style="background:var(--s{i + 1})"></i>{escape(label)}</span>'
            for i, (label, _, _) in enumerate(_SERIES.values())
        )
        sections.append(
            f"<section><h2>{escape(agent)}</h2>"
            f'<div class="legend">{legend}</div>'
            f'<div class="scroll">{_chart(runs)}</div>'
            f"{_runs_table(runs)}</section>"
        )
    if not sections:
        sections.append(
            "<p>No eval history yet. Commit a prompt, model or tool change.</p>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Vikram eval history</title>"
        f"<style>{_CSS}</style></head><body><main>"
        "<header><h1>Vikram eval history</h1>"
        '<p class="muted">Each point is one run. Hover a point for the commit and '
        "what changed. Pass rate = share of cases passing, averaged over repeats.</p>"
        "</header>" + "".join(sections) + "</main></body></html>"
    )


def write_report(repo: Path, out: Path) -> Path:
    records = history.load_records(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(records))
    return out
