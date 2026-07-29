"""The DecisionPacket's image row is named for what a reviewer looks for.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL TSX source (convention: test_conductor_page_animated_
cleanup_ui.py:4-6).

Owner 2026-07-29, reviewing a green gate: "the decision packet should say
visual evidence then have the screenshots in it". The row was titled
"Screenshots", naming the file type rather than the question a reviewer is
asking (can I SEE that this worked?).

NOTE, deliberately: this docstring does NOT name the task id it came from.
Pinned-test discovery (api/tasks.py _discover_pinned_test_rows) is
CONTENT-BASED - it enrols any tests/**/*.py that MENTIONS a task id into
that task's pinned oracle. An earlier draft said "reviewing task <id>'s
gate" and silently added these two UI tests to that task's gate evidence,
taking its pinned count from 6 to 8. A prose mention must never change what
a gate measures.
"""
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
        / "components" / "plan" / "DecisionPacket.tsx")


def _row_tag() -> str:
    """The <Row ...> opening tag that renders the screenshots channel."""
    src = _SRC.read_text(encoding="utf-8")
    marker = "pkt.screenshots.length === 0"
    assert marker in src, "the screenshots Row lost its empty= guard"
    start = src.rindex("<Row", 0, src.index(marker))
    # Walk to the tag's real close: a '>' at brace depth 0. A JSX arrow
    # function inside a prop contains '>' characters that are NOT the end.
    depth = 0
    for i in range(start, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return src[start:i + 1]
    raise AssertionError("unterminated <Row> tag")


def test_the_image_row_is_titled_visual_evidence():
    tag = _row_tag()
    assert 'title="Visual evidence"' in tag, (
        f"the image row must be titled 'Visual evidence'; got: {tag}")
    assert 'title="Screenshots"' not in tag, (
        "the old file-type title must be retired, not left alongside")


def test_the_row_still_renders_the_screenshots_inside_it():
    """Renaming must not detach the row from its images."""
    src = _SRC.read_text(encoding="utf-8")
    body = src[src.index('title="Visual evidence"'):]
    body = body[:body.index("</Row>")]
    assert "EvidenceGallery" in body, (
        "the visual-evidence row must still render the EvidenceGallery")
    assert "pkt.screenshots.map" in body, (
        "the gallery must still be fed from pkt.screenshots")
