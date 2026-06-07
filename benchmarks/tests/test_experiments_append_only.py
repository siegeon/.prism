"""RED — Failing tests for the append-only EXPERIMENTS.md writer (task e043f449).

Pins AC-3 / AC-4 / AC-6 / AC-10: every new measured run (baseline lock + each
vector trial) is appended as a row in benchmarks/EXPERIMENTS.md with a delta vs
baseline and a keep/kill decision; prior rows are NEVER rewritten (append-only);
and rows are built from a MEASURED summary dict (not quoted MemPalace numbers).

Pinned as a real seam: a shared `benchmarks/experiments_log.py` module that
(1) formats a row from a measured-run summary, (2) appends it to a log file
without mutating existing content, and (3) requires a keep/kill decision +
delta-vs-baseline field so a trial can't land undocumented.

Must FAIL until benchmarks/experiments_log.py ships.

[Source: benchmarks/EXPERIMENTS.md — append-only lift log discipline]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_BENCH = _HERE.parent.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))


def _import_log():
    try:
        import experiments_log as mod
        return mod
    except (ImportError, ModuleNotFoundError):
        return None


def test_experiments_log_module_exists():
    """AC-3: a shared append-only EXPERIMENTS writer is shipped."""
    mod = _import_log()
    assert mod is not None, "benchmarks/experiments_log.py not importable — not shipped"
    assert hasattr(mod, "append_row"), "append_row() not exported"
    assert hasattr(mod, "format_row"), "format_row() not exported"


def test_format_row_requires_delta_and_decision():
    """AC-3/AC-10: a row must carry delta-vs-baseline and a keep/kill decision."""
    mod = _import_log()
    assert mod is not None and hasattr(mod, "format_row"), "not shipped"
    summary = {"tag": "locomo-baseline", "metric": "temporal_recall", "value": 0.692}
    with pytest.raises((ValueError, KeyError, TypeError)):
        # No delta / decision supplied → must refuse to format an undocumented row.
        mod.format_row(summary)


def test_append_row_is_append_only(tmp_path):
    """AC-3: appending must preserve every prior byte (no rewrite of old rows)."""
    mod = _import_log()
    assert mod is not None and hasattr(mod, "append_row"), "not shipped"
    log = tmp_path / "EXPERIMENTS.md"
    original = "# Brain improvement experiments\n\n| existing row |\n"
    log.write_text(original, encoding="utf-8")

    summary = {
        "tag": "locomo-temporal-boost",
        "metric": "temporal_recall",
        "value": 0.74,
        "delta": "+0.05",
        "decision": "keep",
        "measured_on": "prism-brain",
    }
    mod.append_row(log, summary)

    after = log.read_text(encoding="utf-8")
    assert after.startswith(original), "prior rows must be preserved verbatim (append-only)"
    assert "locomo-temporal-boost" in after, "new row not appended"
    assert "+0.05" in after and "keep" in after, "delta + decision must appear in the row"


def test_append_row_records_measured_provenance(tmp_path):
    """AC-4: rows assert the number was MEASURED on PRISM Brain, not quoted."""
    mod = _import_log()
    assert mod is not None and hasattr(mod, "append_row"), "not shipped"
    log = tmp_path / "EXPERIMENTS.md"
    log.write_text("# log\n", encoding="utf-8")
    summary = {
        "tag": "convomem-baseline",
        "metric": "recall",
        "value": 0.5,
        "delta": "—",
        "decision": "anchor",
        "measured_on": "prism-brain",
    }
    mod.append_row(log, summary)
    after = log.read_text(encoding="utf-8")
    assert "prism-brain" in after, "row must record measured-on provenance"
