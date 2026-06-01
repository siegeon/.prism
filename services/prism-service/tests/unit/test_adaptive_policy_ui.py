"""RED scaffold — Adaptive policy UI panel (Tier 3, task f6f095e3).

No JS test runner in the web app, so the UI-FIRST acceptance criterion is
pinned by asserting the LearningPage.tsx SOURCE (same pattern as
test_memory_page_activation_ui.py):

  * the page fetches GET /api/learning/policy;
  * it renders an "Adaptive policy" panel showing the 3 tuned knobs
    (forget_cutoff, decay_weight, merge_similarity_threshold);
  * it renders the per-op verdict accuracy table (op_type + accuracy).

FAILS today: LearningPage.tsx has no policy fetch, no knob panel, no
op-accuracy render.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
        / "pages" / "LearningPage.tsx")


def _src() -> str:
    return _WEB.read_text(encoding="utf-8")


def test_learning_page_fetches_policy_endpoint():
    src = _src()
    assert "/api/learning/policy" in src, (
        "LearningPage must fetch GET /api/learning/policy"
    )


def test_learning_page_has_adaptive_policy_panel():
    src = _src()
    assert "Adaptive policy" in src or "Adaptive Policy" in src, (
        "an 'Adaptive policy' panel/SectionLabel must exist"
    )
    # All three tuned knobs must be referenced in the source.
    for knob in ("forget_cutoff", "decay_weight", "merge_similarity_threshold"):
        assert knob in src, f"knob {knob} not rendered in LearningPage"


def test_learning_page_renders_op_accuracy():
    src = _src()
    assert "op_accuracy" in src, (
        "LearningPage must read op_accuracy from the policy response"
    )
    # The per-op rows render op_type + a numeric accuracy.
    assert "op_type" in src, "op-accuracy table must render op_type"
    assert "accuracy" in src, "op-accuracy table must render accuracy"
