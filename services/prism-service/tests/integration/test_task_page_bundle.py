"""RED scaffold — the task page must not ship the diagram stack (task 14d266a1).

Measured on the live daemon: opening a task took ~3s, with index-*.js at 674 kB
(1025 ms) and TaskDetailPage-*.js at 646 kB — ~1.3 MB of JS — while every API
call was under 90 ms. The weight is the front end.

Cause: components/plan/Mermaid.tsx does a top-level `import mermaid from
"mermaid"`; PlanView statically imports Mermaid; TaskDetailPage statically
imports PlanView. So mermaid (and its cytoscape/katex/diagram packs) rides in
the page's critical path for a Diagram TAB that is not even the default.

Owner rule: "anything that is a collapsed box is a lazy load".

Two halves, deliberately: SOURCE assertions catch the regression at review
time, and BUILT-OUTPUT assertions prove the fix actually changed what ships —
a source edit that leaves the bundle identical must NOT pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_PLANVIEW = _WEB_SRC / "components" / "plan" / "PlanView.tsx"
# Task c016667f moved the Design tab's assembled rendering (prototype +
# diagram + doc as ONE card, not one-at-a-time sub-tabs) out of PlanView.tsx
# and into its own component — the lazy Mermaid boundary this file pins
# moved with it. Both are checked below; the invariant (lazy-loaded, still
# renders, still Suspense-wrapped) is unchanged, only its file moved.
_DESIGN_PACKET = _WEB_SRC / "components" / "plan" / "DesignPacket.tsx"
_DIST = _SERVICE_ROOT / "prism_service" / "web_dist" / "assets"

# The pre-change TaskDetailPage chunk. The fix must come in materially under it.
BASELINE_TASKPAGE_KB = 646
MAX_TASKPAGE_KB = 400


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


# ── AC-1 / AC-3: the diagram renderer is reached lazily, not statically ─

def test_planview_imports_mermaid_lazily():
    # task c016667f: the lazy boundary now lives wherever the Design card
    # renders the diagram (DesignPacket.tsx), so both are read and the
    # invariant is checked against whichever one carries it.
    src = _read(_PLANVIEW) + "\n" + _read(_DESIGN_PACKET)
    for p in (_PLANVIEW, _DESIGN_PACKET):
        assert not re.search(r'^import\s+Mermaid\s+from\s+["\']\./Mermaid["\']',
                             _read(p), re.M), (
            f"{p.name} must NOT statically import Mermaid — that edge is "
            "what drags the whole mermaid stack into the task page chunk")
    assert re.search(r'lazy\(\s*\(\)\s*=>\s*import\(\s*["\']\./Mermaid["\']',
                     src), (
        "the task page must obtain Mermaid via lazy(() => import('./Mermaid'))")


def test_no_static_mermaid_package_import_on_the_task_page_path():
    """Nothing PlanView/TaskDetailPage pull in may import the package itself."""
    offenders = []
    for p in (_WEB_SRC / "pages" / "TaskDetailPage.tsx", _PLANVIEW,
             _DESIGN_PACKET):
        if re.search(r'^import\s+[^;\n]*\bfrom\s+["\']mermaid["\']',
                     _read(p), re.M):
            offenders.append(p.name)
    assert offenders == [], (
        f"{offenders} statically import the mermaid package on the task-page "
        "path; it must arrive only through the lazy boundary")


# ── AC-2: the tab still renders a diagram (not an empty box) ───────────

def test_the_diagram_still_renders_behind_a_suspense_boundary():
    # task c016667f: the Diagram render site moved into DesignPacket.tsx
    # (the Design tab's ONE assembled card); pin it there.
    src = _read(_DESIGN_PACKET)
    assert "<Mermaid" in src, (
        "the Diagram tab must still RENDER the diagram — removing it is a "
        "regression, not a fix")
    assert "Suspense" in src, (
        "the lazy diagram needs a Suspense boundary or the tab throws while "
        "its chunk loads")
    # The boundary must actually wrap the render site.
    idx = src.index("<Mermaid")
    before = src[:idx]
    assert before.rindex("<Suspense") > before.rindex("</Suspense") if "</Suspense" in before else True, (
        "the <Mermaid> render site must be INSIDE a Suspense boundary")


# ── AC-4 / AC-5: what actually SHIPS changed ──────────────────────────

def _chunks() -> dict[str, int]:
    assert _DIST.is_dir(), (
        f"no built assets at {_DIST} — run `npm --prefix "
        "services/prism-service/prism_service/web run build` first")
    return {p.name: p.stat().st_size for p in _DIST.glob("*.js")}


def test_mermaid_ships_in_its_own_chunk_not_the_task_page_chunk():
    chunks = _chunks()
    page = [n for n in chunks if n.startswith("TaskDetailPage-")]
    assert page, f"no TaskDetailPage chunk in {sorted(chunks)[:12]}"

    # The diagram code must be emitted as its OWN chunk...
    mermaid_chunks = [n for n in chunks if n.startswith("Mermaid-")]
    assert mermaid_chunks, (
        f"no separate Mermaid-*.js chunk emitted; the lazy boundary did not "
        f"split it out. chunks: {sorted(chunks)[:12]}")
    assert chunks[mermaid_chunks[0]] > 200 * 1024, (
        "the Mermaid chunk is suspiciously small — the library may still be "
        "bundled elsewhere")

    # ...and the page chunk may reference it ONLY by module specifier (the
    # lazy import resolves through the emitted filename), never carry the code.
    page_src = (_DIST / page[0]).read_text(encoding="utf-8", errors="ignore")
    stray = [m.start() for m in re.finditer(r"mermaid", page_src, re.I)
             if not re.match(r"Mermaid-[A-Za-z0-9_-]+\.js",
                             page_src[m.start():m.start() + 40])]
    assert stray == [], (
        f"the task page chunk carries mermaid code at {len(stray)} site(s), "
        "not just the lazy chunk's filename — the boundary did not hold")


def test_task_page_chunk_is_materially_smaller():
    chunks = _chunks()
    page = [n for n in chunks if n.startswith("TaskDetailPage-")]
    assert page, "no TaskDetailPage chunk emitted"
    kb = chunks[page[0]] / 1024
    assert kb < MAX_TASKPAGE_KB, (
        f"TaskDetailPage chunk is {kb:.0f} kB; it was {BASELINE_TASKPAGE_KB} kB "
        f"before this fix and must come in under {MAX_TASKPAGE_KB} kB")
