"""UI-FIRST acceptance test for: Make the Tasks timeline readable with rich
markdown (task f01b6619-37ef-4d40-bbad-993fdda14a85).

The web app ships no JS test runner, so — exactly as test_explore_narrative_ui
and the other *_ui.py tests do — the UI-FIRST acceptance criteria are pinned by
asserting over the TSX SOURCE. This is the real wiring seam, not an isolated
unit: the consumer pages must actually IMPORT and RENDER the shared <Markdown>
component (a Markdown.tsx that merely exists but is wired into nothing is a
dead false-green this suite has shipped before).

FAILS today because:
  - web/src/components/Markdown.tsx does not exist yet (FR-1/FR-2/FR-3);
  - TaskDetailPage.tsx still renders task.description in a raw <pre> (FR-4);
  - TaskTextPage.tsx still routes description through <EvidenceView> (FR-5);
  - TasksPage.tsx board cards render NO description body at all (FR-5);
  - parseMarkdown()/highlight() parser logic still lives in two files (AC-1).
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_MARKDOWN = _WEB / "components" / "Markdown.tsx"
_DETAIL = _WEB / "pages" / "TaskDetailPage.tsx"
_TEXT = _WEB / "pages" / "TaskTextPage.tsx"
_TASKS = _WEB / "pages" / "TasksPage.tsx"
_ONBOARD = _WEB / "components" / "understand" / "OnboardingView.tsx"
_EVIDENCE = _WEB / "components" / "EvidenceView.tsx"
_PKG = _WEB.parent / "package.json"  # web/package.json (one above src/)


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


# ---- FR-1 / AC-1: ONE shared renderer, no duplicated parser logic ----------

def test_shared_markdown_component_exists():
    assert _MARKDOWN.exists(), \
        "FR-1: web/src/components/Markdown.tsx must exist as the single renderer"
    src = _read(_MARKDOWN)
    assert "export default function Markdown" in src \
        or "export function Markdown" in src, \
        "Markdown.tsx must export a Markdown component"


def test_parser_logic_lives_only_in_markdown():
    # AC-1: a grep over web/src/components shows parseMarkdown / highlight()
    # block+token logic lives ONLY in Markdown.tsx. OnboardingView was deleted
    # with the verified-orphan understand/* cluster (ui-redesign 16777a76) —
    # its absence satisfies the no-duplicate rule by construction; EvidenceView
    # must still delegate rather than keep its own copy.
    md = _read(_MARKDOWN)
    assert "parseMarkdown" in md or "function parse" in md, \
        "the block parser must live in Markdown.tsx"

    assert not _ONBOARD.exists(), \
        "AC-1: OnboardingView.tsx was deleted as an orphan; do not resurrect"
    evidence = _read(_EVIDENCE)
    assert "function highlight" not in evidence, \
        "AC-1: highlight() body must NOT remain in EvidenceView.tsx"


# ---- FR-2: structured block parsing (headings / lists / code / bold) --------

def test_markdown_parses_structured_blocks():
    md = _read(_MARKDOWN)
    # H1/H2/H3 headings rendered as distinct heading elements.
    for tag in ("<h1", "<h2", "<h3"):
        assert tag in md, f"FR-2: Markdown must render {tag} headings"
    # Unordered lists as real <ul>/<li>, not a run-on paragraph.
    assert "<ul" in md and "<li" in md, "FR-2: lists must render as <ul>/<li>"
    assert "<p" in md, "FR-2: paragraphs must render as <p> blocks"
    # Inline `code` and **bold**. Since v6.7.13 backtick spans render via the
    # shared <XrefLink> code chip (clickable, resolve-aware) instead of a
    # literal <code> element — both satisfy FR-2's "distinct code affordance".
    assert "<code" in md or "<XrefLink" in md, \
        "FR-2: inline `code` spans must render as <code> or <XrefLink> chips"
    assert "<strong" in md, "FR-2: **bold** must render as <strong>"
    # The heading/list/bold detection regexes carried over from the block parser.
    assert "^#" in md or r"#\s" in md, "FR-2: heading regex must be present"
    assert r"\*\*" in md, "FR-2: **bold** regex must be present"


# ---- FR-3 / AC-3: verdict token color-coding preserved ----------------------

def test_verdict_tokens_color_coded():
    md = _read(_MARKDOWN)
    # PASSED/GREEN/'N passed' -> emerald, warning -> amber, FAIL(ED) -> rose.
    assert "PASSED" in md and "GREEN" in md, \
        "FR-3: emerald result tokens (PASSED/GREEN) must be recognised"
    assert "FAIL" in md, "FR-3: FAIL/FAILED must be recognised (rose)"
    assert "warning" in md, "FR-3: warning token must be recognised (amber)"
    assert "passed" in md, "FR-3: 'N passed' token must be recognised"
    for tone in ("emerald", "amber", "rose"):
        assert f"--accent-{tone}-fg" in md, \
            f"FR-3/AC-6: {tone} tone must come from var(--accent-{tone}-fg)"


# ---- FR-4 / AC-4: TaskDetailPage description <pre> replaced by <Markdown> ----

def test_detail_description_uses_markdown_not_pre():
    src = _read(_DETAIL)
    assert 'import Markdown from "@/components/Markdown"' in src \
        or "components/Markdown" in src, \
        "FR-4: TaskDetailPage must import the shared <Markdown>"
    assert "<Markdown" in src, "FR-4: TaskDetailPage must render <Markdown>"
    # AC-4: the DESCRIPTION must not be a raw <pre> dump.
    #
    # (narrowed) This pair used to read `"whitespace-pre-wrap" not in src` and
    # `"<pre" not in src` — a file-WIDE ban on the string. That was only ever
    # a proxy for "the description block is gone", and it held right up until
    # the page grew a second, legitimate <pre>: the inline text-artifact
    # viewer (~L597) that fetches a log/receipt and shows it as monospace
    # text. A log viewer SHOULD be a <pre whitespace-pre-wrap>; that is the
    # correct tag for preformatted output. So the old assertions failed on
    # code that is right, while the actual contract (renders
    # <Markdown text={task.description} />) was satisfied the whole time.
    # Pin the contract itself: no <pre> may wrap the description.
    #
    # SUPERSEDED 2026-08-26 (task 6968cc39): the call now reads
    # `<Markdown text={linkedDescription || task.description} />` -- the
    # description falls back to task.description only when no
    # entity-linked version exists. The contract (description renders
    # through <Markdown>, task.description is still the ultimate source)
    # is unchanged; only the literal shape moved.
    assert re.search(r"<Markdown\s+text=\{[^}]*task\.description[^}]*\}", src), \
        "AC-4: the description must render through <Markdown>, not a raw <pre>"
    for block in re.findall(r"<pre\b.*?</pre>", src, flags=re.S):
        assert "task.description" not in block, \
            "AC-4: the description must not be dumped inside a <pre> block"


# ---- FR-5 / AC-5: TaskTextPage description route + /tasks list use Markdown --

def test_text_page_description_routes_through_markdown():
    src = _read(_TEXT)
    assert "<Markdown" in src, \
        "FR-5: TaskTextPage description route must render <Markdown>"
    assert "components/Markdown" in src, \
        "FR-5: TaskTextPage must import the shared <Markdown>"


def test_task_description_renders_via_shared_markdown():
    # FR-5 re-pinned to the shipped design: the board became the artifact's
    # title-row grouped table (ui-redesign 16777a76 — descriptions no longer
    # render in the list view by design); the description's rich-markdown
    # rendering lives on the task DETAIL page via the ONE shared renderer.
    src = _read(_DETAIL)
    assert "description" in src, \
        "FR-5: task detail must carry the description"
    assert "<Markdown" in src, \
        "FR-5: the description body must render via the shared <Markdown>"
    assert "components/Markdown" in src, \
        "FR-5: TaskDetailPage must import the shared <Markdown>"


# ---- NFR-1 / AC-6: colors from Hermes CSS vars, no hardcoded bypass ---------

import re


def test_markdown_no_hardcoded_color_bypass():
    md = _read(_MARKDOWN)
    # No text-(emerald|amber|rose)-* tailwind color classes — tones must come
    # from var(--accent-*-fg), not a tailwind palette bypass.
    bad = re.findall(r"text-(?:emerald|amber|rose|green|red|yellow)-\d", md)
    assert not bad, f"NFR-1/AC-6: hardcoded tailwind color classes: {bad}"
    # No raw hex colors.
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", md), \
        "NFR-1/AC-6: no hardcoded hex colors in Markdown.tsx"
    # Tones flow from CSS vars.
    assert "var(--accent-" in md, \
        "NFR-1/AC-6: token tones must read var(--accent-*-fg)"


# ---- NFR-2 / AC-6: no heavy markdown dependency added -----------------------

def test_no_heavy_markdown_dependency():
    pkg = _read(_PKG)
    for dep in ("streamdown", "shiki", "prismjs", "highlight.js"):
        assert f'"{dep}"' not in pkg, \
            f"NFR-2/AC-6: heavy markdown dep '{dep}' must NOT be added"


# ---- NFR-3 / AC-7: PRISM_VERSION patch-bumped in the same commit ------------

_VERSION = (Path(__file__).resolve().parent.parent.parent
            / "prism_service" / "__version__.py")


def test_version_patch_bumped_past_650():
    src = _VERSION.read_text(encoding="utf-8")
    m = re.search(r'PRISM_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, "PRISM_VERSION must be a semver string"
    major, minor, patch = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # Baseline at scaffold time was 6.5.0; the impl commit must patch-bump it.
    assert (major, minor, patch) > (6, 5, 0), \
        f"NFR-3: PRISM_VERSION must be patch-bumped past 6.5.0 (got {m.group(0)})"
