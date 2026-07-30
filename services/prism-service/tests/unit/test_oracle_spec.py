"""oracle_spec.OracleSpec.from_task — the visual-observable floor
(task ee5b226d, owner-recorded recurrence of "browser oracle keeps eating
the human-sign-off gate").

THE DEFECT: ``from_task`` branched on the FIRST URL found in the oracle text
— the pytest rung aside, ANY oracle citing a URL derived ``ADAPTER_HTTP``,
even when the oracle text names a VISUAL observable (a screenshot, a
skeleton, a tint, an absent row) that an http_probe's assertions
(status<400, non-empty body, no error token, bounded latency) structurally
cannot see. ``ADAPTER_BROWSER`` was only reached when NO url was present at
all. Reproduced live on task 89e90d1a (skeleton/tint/screenshot oracle) and
freshly on an unrelated GitHub-sync task whose oracle asked that no PR title
appear as an imported task — both derived http_probe on evidence that
cannot observe their own subject.

THE FIX: ``_names_visual_observable`` keys on the OBSERVABLE the oracle text
names (a vocabulary of visual cues, plus a "no/not ... appear[s]" /
"appears as a" shape for presence/absence claims). When it fires, the URL
branch is skipped even though a URL is present, so the spec falls through
to the ``ADAPTER_BROWSER`` branch (manual_evidence_required) — the honest
boundary a human's review or a real browser render satisfies, never a
silent http-probe pass.

likely_misfire (recorded on the task, pinned here): over-correcting so that
EVERY oracle containing a URL becomes manual_evidence_required, stranding
genuinely machine-checkable reachability tickets on a human click. Both
directions are asserted below — the negative case (AC-2) is the test that
would fail if the fix were a blanket "any URL -> browser".
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import oracle_spec as osp  # noqa: E402


def _task(oracle, **kw):
    return SimpleNamespace(
        id=kw.pop("id", "t1"), oracle=oracle,
        likely_misfire=kw.pop("likely_misfire", ""),
        proof_type=kw.pop("proof_type", ""),
        verify=kw.pop("verify", []), tags=kw.pop("tags", []), **kw)


# ---------------------------------------------------------------------------
# AC-1 — a visual-observable oracle does NOT derive http_probe, even with
# a URL present
# ---------------------------------------------------------------------------

# Same shape as the recorded 89e90d1a recurrence (skeleton/tint/screenshot),
# with a dev URL cited alongside — the exact shape that used to degrade.
_VISUAL_ORACLE = (
    "Open http://127.0.0.1:8888/tasks?project=prism and confirm the LiveBar "
    "shows skeleton/loading placeholders and holds a neutral loading tint "
    "while data streams in. A screenshot captured into the task evidence "
    "store proves NO 0-valued KPI tile is ever shown."
)


def test_visual_observable_oracle_with_url_derives_browser_not_http():
    spec = osp.OracleSpec.from_task(_task(_VISUAL_ORACLE))
    assert spec.adapter == osp.ADAPTER_BROWSER, (
        f"a skeleton/tint/screenshot oracle must not derive http_probe, "
        f"got {spec.adapter!r}")


def test_visual_observable_oracle_is_human_judgment():
    spec = osp.OracleSpec.from_task(_task(_VISUAL_ORACLE))
    assert osp.is_human_judgment(spec) is True, (
        "a visual oracle must classify as human-judgment so the "
        "adjudicator declines and readiness reports the sign-off path")


# Same shape as the recorded fresh recurrence: a presence/absence claim
# about what's RENDERED (no PR title should appear as a task row), with a
# URL cited — none of the plain visual-cue words (screenshot/skeleton/
# tint/...) appear here, so this pins the negated-appearance detector
# specifically, not just the keyword list.
_ABSENT_ROW_ORACLE = (
    "Open http://127.0.0.1:8888/tasks?project=prism and confirm no "
    "imported PR title appears as a task in the list."
)


def test_negated_appearance_oracle_with_url_derives_browser():
    spec = osp.OracleSpec.from_task(_task(_ABSENT_ROW_ORACLE))
    assert spec.adapter == osp.ADAPTER_BROWSER, (
        "an oracle asserting a row must NOT appear cannot be proven by an "
        f"http_probe (status/body/latency only), got {spec.adapter!r}")
    assert osp.is_human_judgment(spec) is True


def test_appears_as_a_shape_is_also_caught():
    spec = osp.OracleSpec.from_task(_task(
        "Visit http://127.0.0.1:8888/tasks and check the merged PR appears "
        "as a done task with the right title."))
    assert spec.adapter == osp.ADAPTER_BROWSER


# ---------------------------------------------------------------------------
# AC-2 — a genuine reachability oracle still DOES derive http_probe (the
# likely_misfire guard: this is the test that fails on a blanket over-fix)
# ---------------------------------------------------------------------------

def test_reachability_oracle_with_url_still_derives_http_probe():
    spec = osp.OracleSpec.from_task(_task(
        "GET http://127.0.0.1:8888/api/version returns 200 with the "
        "current build info"))
    assert spec.adapter == osp.ADAPTER_HTTP, (
        f"a plain reachability oracle must still derive http_probe, "
        f"got {spec.adapter!r}")


def test_reachability_oracle_is_not_human_judgment():
    spec = osp.OracleSpec.from_task(_task(
        "the health endpoint at http://127.0.0.1:8888/api/version is "
        "reachable and returns 200"))
    assert osp.is_human_judgment(spec) is False


def test_board_serves_reachability_oracle_unaffected():
    """Regression guard: an existing neighbouring-suite shape (a bare board
    URL + 'serves') must keep deriving http_probe."""
    spec = osp.OracleSpec.from_task(_task(
        "http://127.0.0.1:8888/tasks serves the board"))
    assert spec.adapter == osp.ADAPTER_HTTP


# ---------------------------------------------------------------------------
# Ordering guards — the visual floor must not disturb the other rungs
# ---------------------------------------------------------------------------

def test_test_proof_type_still_wins_over_visual_language():
    """A test-proofed task's pinned pytest ids still derive pytest_ids even
    if the oracle prose happens to use visual words — the pytest rung is
    checked BEFORE the URL/visual branch and must stay first."""
    spec = osp.OracleSpec.from_task(_task(
        "screenshot shows the skeleton fades — see it render",
        proof_type="test", verify=["services/x/tests/test_y.py::test_z"]))
    assert spec.adapter == osp.ADAPTER_PYTEST


def test_no_url_visual_oracle_unaffected_by_the_new_rule():
    """The pre-existing no-URL -> browser path must be unchanged."""
    spec = osp.OracleSpec.from_task(_task("you can SEE the dashboard render"))
    assert spec.adapter == osp.ADAPTER_BROWSER


def test_browser_target_still_carries_the_oracle_url_for_the_runner():
    """The browser branch sets target=oracle.strip() (not just the bare
    cue), so browser_oracle_runner._extract_url can still find and load the
    cited URL for its own real capture."""
    from prism_service.services import browser_oracle_runner as bor
    spec = osp.OracleSpec.from_task(_task(_VISUAL_ORACLE))
    assert bor._extract_url(spec) == "http://127.0.0.1:8888/tasks?project=prism"
