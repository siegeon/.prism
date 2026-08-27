"""Guard: retired surfaces stay retired (task 292e8ea2, AC-1/AC-2/AC-3/AC-5).

The removal sweep must leave behind a LEDGER, ``docs/removal-ledger.md``,
that names every candidate with the evidence it is dead. This test is driven
by that ledger rather than a hand-maintained list, so every future REMOVED
entry extends the assertion set automatically.

Ledger contract (markdown table rows, anywhere in the file)::

    | REMOVED | `path/or/symbol` | <evidence: the search that came back empty> |
    | KEEP    | `path/or/symbol` | <the reference that keeps it> |

For every REMOVED name the test asserts, against ``git ls-files`` and
``git grep``, that NO tracked file still carries or references it (source,
tests, docs, workflows, packaging manifests) — the ledger and this file are
the only permitted mentions. Every candidate the ticket itself named must be
classified one way or the other; nothing is left in the ambiguous middle.

RED against the current tree: the ledger does not exist, so the first assert
fails on a genuine assertion (not an exception); once the ledger lands, the
per-name assertions fail until each REMOVED surface is actually gone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[4]  # tests/unit -> tests -> prism-service -> services -> repo
_LEDGER = _REPO / "docs" / "removal-ledger.md"
_ROW = re.compile(r"^\|\s*(REMOVED|KEEP)\s*\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*$", re.M)

# Candidates the ticket named up front; each MUST be classified in the ledger.
_TICKET_CANDIDATES = [
    "services/prism-service/desktop/tauri-shell",
    "services/prism-service/docker-compose.v51.yml",
    "docs/tasks/PLAT-0042-T3-smoke-pool-instrumentation.md",
    "docs/tasks/PLAT-0042-T4-run-experiments.md",
    "docs/tasks/PLAT-0042-T5-operator-doc.md",
    "docs/stories",
]
_SELF = {"docs/removal-ledger.md", _HERE.relative_to(_REPO).as_posix()}


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True)
    return out.stdout


def _entries() -> list[tuple[str, str, str]]:
    assert _LEDGER.is_file(), (
        f"removal ledger missing: {_LEDGER.relative_to(_REPO)} must be committed "
        "before any surface is retired (AC-1)"
    )
    rows = _ROW.findall(_LEDGER.read_text(encoding="utf-8"))
    assert rows, "removal ledger has no `| REMOVED/KEEP | `name` | evidence |` rows"
    return rows


def _removed() -> list[tuple[str, str]]:
    return [(n, ev) for d, n, ev in _entries() if d == "REMOVED"]


def test_ledger_exists_with_evidence_on_every_row():
    for decision, name, evidence in _entries():
        assert evidence.strip(), f"{decision} `{name}` has no evidence/reference (AC-1)"


def test_every_ticket_candidate_is_classified():
    classified = {n.rstrip("/") for _, n, _ in _entries()}
    missing = [c for c in _TICKET_CANDIDATES if c not in classified]
    assert not missing, f"ledger leaves ticket candidates unclassified: {missing}"


def test_removed_paths_are_no_longer_tracked():
    tracked = _git("ls-files").splitlines()
    for name, _ in _removed():
        still = [p for p in tracked if p == name or p.startswith(name.rstrip("/") + "/")]
        assert not still, f"REMOVED `{name}` is still tracked: {still[:5]}"


def test_removed_names_are_not_referenced_anywhere_tracked():
    for name, _ in _removed():
        key = name.rstrip("/").rsplit("/", 1)[-1]
        hits = set(_git("grep", "-l", "-F", "--", key).splitlines()) - _SELF
        assert not hits, f"REMOVED `{name}` ({key!r}) still referenced by: {sorted(hits)[:8]}"


def test_no_removed_row_is_also_kept():
    seen: dict[str, str] = {}
    for decision, name, _ in _entries():
        assert seen.setdefault(name, decision) == decision, f"`{name}` is both REMOVED and KEEP"
