"""RED scaffold — durable per-test run outcomes (task f3e8d477).

The evidence package must be able to answer "did the tests run, and did they
pass?". Today a run's statuses are computed and thrown away, so the TESTS tab
prints NOT RUN forever. These pin the store that makes a run outlive its
response, plus the hydration rules: a record from another tree is STALE, and a
test with no record is never shown as a pass.

Imports live INSIDE the tests so the file collects and fails at runtime (red =
rc 1) before services/test_run_store.py exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

TASK = "f3e8d477-5912-4839-9b1d-504d748e1bc9"
TREE_A = "aaaaaaaaaaaa"
TREE_B = "bbbbbbbbbbbb"


def _store(tmp_path, name="test_runs.db"):
    from prism_service.services.test_run_store import TestRunStore

    return TestRunStore(str(tmp_path / name))


def _rows():
    return [
        {"name": "test_alpha", "file": "tests/unit/test_x.py", "status": "passed"},
        {"name": "test_beta", "file": "tests/unit/test_x.py", "status": "failed"},
    ]


# ── AC-1: a run is persisted per test ──────────────────────────────────

def test_record_run_persists_status_tree_and_time(tmp_path):
    store = _store(tmp_path)
    store.record_run(TASK, TREE_A, _rows())

    got = store.statuses_for(TASK)
    assert set(got) == {"test_alpha", "test_beta"}
    assert got["test_alpha"]["status"] == "passed"
    assert got["test_beta"]["status"] == "failed"
    assert got["test_alpha"]["tree_sha"] == TREE_A
    assert got["test_alpha"]["ran_at"], "a run must carry when it ran"


def test_a_later_run_replaces_the_earlier_one(tmp_path):
    store = _store(tmp_path)
    store.record_run(TASK, TREE_A, [{"name": "test_alpha", "status": "failed"}])
    store.record_run(TASK, TREE_B, [{"name": "test_alpha", "status": "passed"}])
    got = store.statuses_for(TASK)
    assert got["test_alpha"]["status"] == "passed"
    assert got["test_alpha"]["tree_sha"] == TREE_B


# ── AC-2: outcomes survive a restart ───────────────────────────────────

def test_outcomes_survive_store_reconstruction(tmp_path):
    _store(tmp_path).record_run(TASK, TREE_A, _rows())
    reopened = _store(tmp_path)  # same file, fresh object/connection
    assert reopened.statuses_for(TASK)["test_alpha"]["status"] == "passed"


# ── AC-5 / AC-6: hydration rules ───────────────────────────────────────

def test_hydrate_stamps_status_from_the_record(tmp_path):
    from prism_service.services.test_run_store import hydrate

    store = _store(tmp_path)
    store.record_run(TASK, TREE_A, _rows())
    rows = [{"name": "test_alpha", "file": "tests/unit/test_x.py"}]
    out = hydrate(store, TASK, rows, TREE_A)
    assert out[0]["status"] == "passed"
    assert out[0].get("stale") is False
    assert out[0].get("ran_at")


def test_a_run_from_another_tree_is_stale_not_current(tmp_path):
    from prism_service.services.test_run_store import hydrate

    store = _store(tmp_path)
    store.record_run(TASK, TREE_A, _rows())
    out = hydrate(store, TASK, [{"name": "test_alpha"}], TREE_B)
    assert out[0].get("stale") is True, (
        "a record from a different tree must read as stale, never as current")


def test_a_test_with_no_record_is_not_run_never_passed(tmp_path):
    from prism_service.services.test_run_store import hydrate

    store = _store(tmp_path)
    out = hydrate(store, TASK, [{"name": "test_never_ran"}], TREE_A)
    assert out[0].get("status") in (None, "", "not-run")
    assert out[0].get("status") != "passed"
    assert not out[0].get("passed")


def test_hydrate_does_not_clobber_an_existing_status(tmp_path):
    """The endpoint already fills some rows from a gate receipt; hydration must
    not overwrite a status that is already known."""
    from prism_service.services.test_run_store import hydrate

    store = _store(tmp_path)
    store.record_run(TASK, TREE_A, [{"name": "test_alpha", "status": "failed"}])
    out = hydrate(store, TASK, [{"name": "test_alpha", "status": "passed",
                                 "verified_by": "gate-receipt"}], TREE_A)
    assert out[0]["status"] == "passed"
