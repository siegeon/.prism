"""Mirror links are badges, not description text (task 675dd11a).

FR-1: a push mirror must stop appending 'Mirrored to {provider} {issue}.\\n
{url}' prose to task.description (task_mirror.py:126) -- the durable badge
already comes from the store link (task 6fbbec35), so the description write
was always a second, redundant, and now-unwanted channel.
FR-2: the stale _record_backlink docstring (which argues the URL "must" be
derivable from the description) is corrected to describe that text as
legacy derivation input only, never the live badge source.
FR-3/FR-4: TaskDetailPage.tsx's Description card and TaskTextPage.tsx's
/tasks/:id/description route must stop feeding the raw task.description
straight into <Markdown>, and instead pass it through a new
stripMirrorProvenance() render-side filter.
FR-5: that helper lives once in web/src/lib/format.ts.
FR-6: nothing here may rewrite STORED descriptions -- this is a render-only
fix, so a legacy row's provenance prose survives untouched and
api/tasks.py's _mirror_url keeps deriving from it.
Owner revision 2026-08-12: the mirror badge row becomes one TITLED BUTTON
per active task.mirrors[] entry, with ALL metadata (including the
synced-ago stamp) rendered INSIDE the button, not floating beside it.

FAILS TODAY because none of stripMirrorProvenance/the corrected docstring/
the button-styled badge exist yet -- _record_backlink still appends the
prose (task_mirror.py:126), TaskDetailPage/TaskTextPage still feed
task.description straight to <Markdown>, and the badge is a bare <a> with
a SIBLING <span> for the synced stamp, not a bordered button with the
stamp nested inside. RED with a trace, by design (write_failing_tests).

ASSERT THE AFFORDANCE A PERSON USES, never the constant behind it -- the
TSX assertions below parse the enclosing JSX block (never a fixed
character window) per the mx-5f916a escaped-slash-guard trap documented in
CLAUDE.md. AC-3 (real running-page screenshots) and AC-6 (a diff check
excluding work_item_sync.py) are procedural checks that belong to the
verify_green_state step, not this file -- they need a live daemon / git
history that does not exist at write_failing_tests time.
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

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_DETAIL_TSX = _WEB / "pages" / "TaskDetailPage.tsx"
_TEXT_TSX = _WEB / "pages" / "TaskTextPage.tsx"
_TASKS_TSX = _WEB / "pages" / "TasksPage.tsx"
_FORMAT_TS = _WEB / "lib" / "format.ts"
_TASK_MIRROR_PY = (_SERVICE_ROOT / "prism_service" / "services"
                    / "task_mirror.py")

REPO = "siegeon/.prism"
SCOPE = "personal-local-user"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _balanced_block(src: str, anchor: str) -> str:
    """From the FIRST '(' at/after `anchor`, return the text up to its
    matching ')' (paren-balanced). Used to isolate the mirrors .map(...)
    JSX return block so assertions read the ENCLOSING branch, never a fixed
    character window (CLAUDE.md: a comment above an element has satisfied
    look-alike window assertions before)."""
    start = src.index(anchor)
    open_at = src.index("(", start)
    depth = 0
    i = open_at
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
        i += 1
    raise AssertionError(f"unbalanced parens scanning from {anchor!r}")


class _FakeGitHub:
    """Stands in for the network only (pattern shared with
    test_task_mirror_production_wiring.py's _FakeGitHub) -- the real
    task_mirror/adapter-registry wiring stays production."""

    provider = "github"

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        return ExternalEntityInput(
            entity_kind="issue", remote_id="I_node_414", display_key="#414",
            title=title, body=body,
            url=f"https://github.com/{REPO}/issues/414",
            remote_status="open", status_category="open",
            remote_updated_at="2026-08-12T00:00:00Z")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Real IntegrationStore/IntegrationOutbox/TaskService, real
    task_mirror.mirror_task -- only the HTTP transport (the fake adapter
    below) is faked, same discipline as test_task_mirror_production_wiring
    .py's `wired` fixture."""
    from prism_service.services import task_mirror
    from prism_service.services.integration_outbox import IntegrationOutbox
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))
    connection = store.ensure_connection(SCOPE, "github", "siegeon",
                                         display_name="siegeon")
    store.ensure_container(SCOPE, connection.id, "repository", REPO,
                           display_key=REPO, display_name=REPO,
                           url=f"https://github.com/{REPO}")
    svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="prism")

    monkeypatch.setattr(task_mirror, "personal_scope", lambda: SCOPE)
    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda project: type("Ctx", (), {"task_svc": svc})())
    monkeypatch.setattr(
        "prism_service.services.integration_store.get_integration_store",
        lambda: store)
    monkeypatch.setattr(
        "prism_service.services.integration_outbox.get_outbox", lambda: outbox)
    monkeypatch.setattr(
        "prism_service.services.sync_prefs.get_sync_preferences",
        lambda: type("P", (), {"enabled": staticmethod(lambda w, p: True)})())
    monkeypatch.setitem(
        __import__("prism_service.api.integrations", fromlist=["_adapters"])
        ._adapters, "github", _FakeGitHub())
    return svc, store


# ---------------------------------------------------------------------------
# AC-1: a new push mirror leaves no 'Mirrored to' prose in the description,
# while still producing an active store link.
# ---------------------------------------------------------------------------

def test_new_push_writes_no_prose(wired):
    from prism_service.services import task_mirror

    svc, store = wired
    task = svc.create(title="ship the badge fix")
    outcome = task_mirror.mirror_task("prism", task.id)
    assert outcome.created is True, outcome.reason

    after = svc.get(task.id)
    text = after.description or ""
    assert "Mirrored to" not in text, (
        "FR-1: task_mirror._record_backlink must stop appending the "
        f"'Mirrored to ...' line to task.description, got: {text!r}")
    assert "github.com" not in text, (
        "FR-1: the mirror URL must not land in description text either, "
        f"got: {text!r}")

    links = store.list_links(SCOPE, task_id=task.id)
    active = [l for l in links if l.state == "active"]
    assert active, (
        "the durable store link must still exist -- the push path itself "
        "(work_item_sync push) is unaffected by this render-only slice")
    entity = store.get_entity(SCOPE, active[0].entity_id)
    assert entity is not None and entity.url == (
        f"https://github.com/{REPO}/issues/414")


# ---------------------------------------------------------------------------
# AC-2: a legacy task's STORED description is byte-identical before/after
# (never rewritten -- render-only fix), and _mirror_url() keeps deriving
# its URL from that same raw text (FR-6).
# ---------------------------------------------------------------------------

def test_legacy_description_byte_identical_and_mirror_url_still_derives(wired):
    from prism_service.api.tasks import _mirror_url

    svc, _store = wired
    legacy_body = (
        "Some real notes about this task.\n\n"
        "Mirrored to github #99.\n"
        f"https://github.com/{REPO}/issues/99"
    )
    task = svc.create(title="legacy row", description=legacy_body)
    before = svc.get(task.id).description

    # Read-path traffic a legacy row sees in normal use -- none of it may
    # touch the stored description (this is a RENDER-side fix only).
    _ = svc.list()
    after = svc.get(task.id).description

    assert after == before == legacy_body, (
        "FR-6: a legacy task's stored description must never be rewritten "
        "by a backfill/migration")
    assert _mirror_url(after) == f"https://github.com/{REPO}/issues/99", (
        "api/tasks.py's _mirror_url must keep deriving the badge URL from "
        "the raw stored text for legacy rows (no orphaned badges)")


# ---------------------------------------------------------------------------
# FR-2: the stale _record_backlink docstring (rationale: "a URL the server
# can derive from the description") is corrected to say the raw text is
# preserved only as LEGACY derivation input, never the live badge source.
# ---------------------------------------------------------------------------

def test_record_backlink_docstring_says_legacy_input_not_live_source():
    src = _read(_TASK_MIRROR_PY)
    m = re.search(
        r'def _record_backlink\([^)]*\) -> None:\s*"""(.*?)"""',
        src, re.S)
    assert m, "_record_backlink must keep its docstring"
    doc = m.group(1)
    assert "legacy" in doc.lower(), (
        "FR-2: the corrected docstring must describe the description text "
        "as LEGACY derivation input")
    assert not re.search(r"a url the server can derive from the description",
                          doc, re.I), (
        "FR-2: the stale claim that the badge URL 'must' be derivable from "
        "the description is still present -- this is no longer true, the "
        "store link (task 6fbbec35) is the live badge source")


# ---------------------------------------------------------------------------
# FR-5: stripMirrorProvenance lives once in web/src/lib/format.ts, built on
# a LINE-ANCHORED regex (^Mirrored (to|from) \S+ .*\.$ + an optional
# following bare-URL line).
# ---------------------------------------------------------------------------

def test_strip_helper_exported_with_line_anchored_regex():
    src = _read(_FORMAT_TS)
    assert "export function stripMirrorProvenance" in src, (
        "FR-5: stripMirrorProvenance must be exported from format.ts")
    assert "Mirrored (to|from)" in src, (
        "FR-5: the strip regex must be the line-anchored "
        r"^Mirrored (to|from) \S+ .*\.$ shape named in the plan")
    # line-anchored: a JS regex literal with the multiline flag, or an
    # equivalent per-line split -- either way `^`/`$` alone (default flags)
    # would match only the WHOLE-STRING start/end, silently failing to
    # strip a provenance block that isn't the very first/last thing in the
    # description. Require evidence of real line anchoring.
    assert re.search(r"/\^Mirrored[^/]*/\w*m\w*", src) or ".split(" in src, (
        "FR-5: the regex must be applied per-line (multiline flag or an "
        "explicit line split), not matched against the whole string")


def _extracted_provenance_regex(src: str) -> "re.Pattern[str]":
    m = re.search(r"/(\^Mirrored[^/]*)/", src)
    assert m, "could not find the ^Mirrored... regex literal in format.ts"
    return re.compile(m.group(1), re.MULTILINE)


# ---------------------------------------------------------------------------
# AC-5: the extracted regex (Python re.MULTILINE is syntax-compatible with
# the simple JS literal this shape requires) actually discriminates
# provenance lines from legitimate prose that merely mentions the words.
# ---------------------------------------------------------------------------

def test_strip_regex_matches_push_and_import_shapes_only():
    src = _read(_FORMAT_TS)
    pattern = _extracted_provenance_regex(src)

    assert pattern.search("Mirrored to github #414.")
    assert pattern.search("Mirrored from jira PRIS-11.")
    # Legitimate prose containing the words mid-sentence must survive --
    # ^ anchoring means a line that does not START with "Mirrored" can
    # never match.
    assert not pattern.search(
        "We mirrored to github in Q3 and it worked."), (
        "AC-5: the regex must not eat legitimate prose containing "
        "'mirrored to' mid-sentence")
    assert not pattern.search("Mirrored to nothing in particular"), (
        "AC-5: a line missing the trailing period must not match "
        r"(the \.\$ anchor is load-bearing)")


# ---------------------------------------------------------------------------
# FR-3/AC-7: TaskDetailPage.tsx's Description card must render through
# stripMirrorProvenance(), never the bare task.description identifier.
# Supersedes test_tasks_timeline_rich_markdown_ui.py:124's contract, which
# this slice retires below (AC-8).
# ---------------------------------------------------------------------------

def test_detail_description_renders_through_strip_helper():
    src = _read(_DETAIL_TSX)
    assert "stripMirrorProvenance" in src, (
        "FR-3: TaskDetailPage.tsx must import/use stripMirrorProvenance")
    assert "<Markdown text={task.description}" not in src, (
        "FR-3: the Description card must not feed the bare task.description "
        "identifier to <Markdown> -- it must pass it through "
        "stripMirrorProvenance() first")
    assert re.search(
        r"<Markdown\s+text=\{stripMirrorProvenance\(\s*task\.description",
        src), (
        "FR-3: <Markdown text={...}> must wrap task.description in "
        "stripMirrorProvenance(...)")


# ---------------------------------------------------------------------------
# FR-4: TaskTextPage.tsx's /tasks/:id/description route (indirection via
# SECTIONS.description -> task[meta.field] -> <Markdown>) is the SECOND
# render site the story's literal grep for '.description' missed.
# ---------------------------------------------------------------------------

def test_text_page_description_renders_through_strip_helper():
    src = _read(_TEXT_TSX)
    assert "stripMirrorProvenance" in src, (
        "FR-4: TaskTextPage.tsx must import/use stripMirrorProvenance for "
        "the description section")
    assert re.search(
        r'import\s*\{[^}]*stripMirrorProvenance[^}]*\}\s*from\s*"@/lib/format"',
        src), (
        "FR-4: stripMirrorProvenance must be imported from @/lib/format")
    assert re.search(r"stripMirrorProvenance\(", src), (
        "FR-4: stripMirrorProvenance must actually be CALLED (not just "
        "imported) on the text this route renders")


# ---------------------------------------------------------------------------
# Owner revision (2026-08-12): the mirror badge row becomes one TITLED
# BUTTON per active task.mirrors[] entry -- provider+issue label, external
# -link arrow, bordered/filled button affordance with hover state,
# target=_blank, data-external-context preserved, with ALL metadata
# (including the synced-ago stamp) rendered INSIDE the button.
# ---------------------------------------------------------------------------

def test_mirror_badge_row_keeps_data_external_context():
    src = _read(_DETAIL_TSX)
    assert "data-external-context" in src, (
        "the mirror badge row's data-external-context marker must survive "
        "the restyle")


def test_mirror_badge_is_titled_button_with_metadata_inside():
    src = _read(_DETAIL_TSX)
    block = _balanced_block(src, ".map((mirror) =>")

    # It is a real clickable anchor/button, not loose text.
    assert re.search(r"<(a|button)\b", block), (
        "AC-4/AC-7: each mirror entry must render as an <a>/<button> "
        "element, not bare text")
    assert 'href={mirror.url}' in block, (
        "AC-7: the element must link out via href={mirror.url}")
    assert 'target="_blank"' in block, (
        "the button must open the external issue in a new tab"
    )
    # Bordered/filled button affordance with a hover state -- not a bare
    # link. Loose heuristic on the className string (no CSS test runner).
    class_m = re.search(r'className="([^"]*)"', block)
    assert class_m, "the element needs a className carrying button styling"
    cls = class_m.group(1)
    assert "border" in cls, "AC-4: button affordance needs a visible border"
    assert "hover:" in cls, "AC-4: the button needs a hover state"

    # The synced-ago stamp must be INSIDE the anchor/button block, not a
    # sibling floating outside it -- `_balanced_block` already isolated the
    # element's own paren-balanced return, so finding it there is enough.
    assert "relativeTime(mirror.last_synced_at)" in block, (
        "AC-4: the synced-ago stamp must be rendered INSIDE the button "
        "panel, not floating outside it")

    # Provider + issue key label, e.g. 'GitHub #414' / 'Jira PRIS-11' --
    # both mirror.provider and mirror.issue must feed the visible label.
    assert "mirror.provider" in block and "mirror.issue" in block, (
        "AC-4: the button's title label must be built from "
        "provider + issue key ('GitHub #414', 'Jira PRIS-11')")


# ---------------------------------------------------------------------------
# FR-7 sanity: TasksPage.tsx must stay untouched -- description is
# deliberately absent from its board ?fields= projection, so it can't show
# provenance prose and needs no strip-helper wiring.
# ---------------------------------------------------------------------------

def test_tasks_page_never_requests_description():
    src = _read(_TASKS_TSX)
    assert "stripMirrorProvenance" not in src, (
        "FR-7: TasksPage.tsx does not render description and must not "
        "gain the strip helper")
