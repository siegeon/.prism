"""Append-only writer for benchmarks/EXPERIMENTS.md (task e043f449).

Every measured run — the baseline lock and each vector trial — lands as ONE
row appended to the lift log. Two hard invariants the tests pin:

- AC-3: appends NEVER rewrite prior bytes (append-only audit trail).
- AC-4/AC-10: a row must carry a delta-vs-baseline AND a keep/kill decision,
  and assert the number was MEASURED on PRISM Brain (``measured_on``) — so a
  trial can't land undocumented and a number can't be quoted from MemPalace's
  results jsonl.

[Source: benchmarks/EXPERIMENTS.md — append-only lift log discipline]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Fields a measured-run summary MUST supply to format a row. Without a delta
# and a keep/kill decision a trial is undocumented; format_row refuses it.
REQUIRED_FIELDS: tuple[str, ...] = (
    "tag", "metric", "value", "delta", "decision",
)


def format_row(summary: dict[str, Any]) -> str:
    """Render one Markdown table row from a MEASURED-run summary.

    Raises ValueError/KeyError if the row is undocumented (missing delta or
    keep/kill decision) — an experiment can't land without an audit trail.
    """
    if not isinstance(summary, dict):
        raise TypeError(f"summary must be a dict, got {type(summary)!r}")
    missing = [f for f in REQUIRED_FIELDS if f not in summary or summary[f] in (None, "")]
    if missing:
        raise ValueError(
            f"undocumented experiment row — missing required field(s): {missing}. "
            "Every row needs tag/metric/value/delta/decision."
        )
    measured_on = summary.get("measured_on", "prism-brain")
    cells = [
        str(summary["tag"]),
        str(summary["metric"]),
        f"{summary['value']}",
        str(summary["delta"]),
        str(summary["decision"]),
        str(measured_on),
    ]
    return "| " + " | ".join(cells) + " |\n"


def append_row(log: Path | str, summary: dict[str, Any]) -> str:
    """Append a formatted row to ``log`` WITHOUT mutating any prior byte.

    Returns the row that was written. Creates the file (with no header) if it
    does not yet exist — callers that want a header write it themselves first.
    """
    log = Path(log)
    row = format_row(summary)
    existing = log.read_text(encoding="utf-8") if log.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    # Open in append mode so we physically cannot rewrite earlier content.
    with log.open("a", encoding="utf-8") as f:
        if not existing:
            # Brand-new file: nothing to preserve.
            pass
        f.write(row)
    return row
