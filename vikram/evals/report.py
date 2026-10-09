"""Build a self-contained, offline HTML report from metrics-only history."""

import json
from pathlib import Path

from vikram.evals.history import DEFAULT_DIRECTORY, atomic_write, load_history


def write_report(directory: Path = DEFAULT_DIRECTORY) -> Path:
    """Rebuild the report without credentials, model calls, or external assets."""
    history = load_history(directory)
    ks = sorted({1, 3} | {k for run in history for k in run.config.ks})
    runs = [run.report_data(ks) for run in history]
    # A model name or label must never be able to close the JSON script element.
    data = json.dumps(runs, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("report.html").read_text(encoding="utf-8")
    script = Path(__file__).with_name("report.js").read_text(encoding="utf-8")
    path = directory / "report.html"
    template = template.replace("/*__REPORT_SCRIPT__*/", script, 1)
    atomic_write(path, template.replace('"__RUN_DATA__"', data, 1))
    return path
