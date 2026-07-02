"""RED scaffold — Brain overview super-node labels must not overprint (task c8b4570e).

The /brain overview (ExplorePage -> the server-rendered /graph/viewer Sigma
canvas) force-renders the <=5 L0 super-node labels: labelDensity is bypassed
for super-nodes (level < 3), so after FA2 settles they overprint each other in
the packed centre ('PRISM Service Core' over 'Devtools Validation Suite',
'Core Base Types' over 'Core Errors And Models'). The fix deconflicts
super-node label placement per frame (offset each label off the ones already
placed) — mirroring the /understand concept graph, whose labels sit clear.

Scans the ACTUAL served Sigma viewer HTML (/graph/viewer) for the deconfliction
seam. FAILS before the fix: the viewer never measures label width, never resets
a per-frame label-rect accumulator, and the label drawer has no super-node
collision-avoidance.
"""

from pathlib import Path

from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
_GRAPH_STATIC = _SERVICE_ROOT / "prism_service" / "routes" / "graph_static.py"


def _viewer_html() -> str:
    from prism_service.main import app

    c = TestClient(app)
    r = c.get("/graph/viewer/prism")
    assert r.status_code == 200, r.text
    return r.text


def test_viewer_measures_label_width_and_resets_per_frame():
    html = _viewer_html()
    # A bounding box needs the label's rendered width; overlap can't be tested
    # without measuring it. Absent before the fix.
    assert "measureText" in html, "super-node label width never measured"
    # The per-frame accumulator of placed label rects must reset each render,
    # else rects leak across frames and every label reads as colliding.
    assert "beforeRender" in html, "no per-frame label-rect reset bound"


def test_viewer_deconflicts_super_node_labels():
    html = _viewer_html().lower()
    # A collision-avoidance accumulator/routine keyed on the super-node labels
    # (the only force-rendered ones). Absent before the fix.
    assert any(tok in html for tok in
               ("superlabelrect", "labelrect", "deconflict")), (
        "no super-node label collision accumulator / deconflict routine in "
        "the served viewer"
    )
