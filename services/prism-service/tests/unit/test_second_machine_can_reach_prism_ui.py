"""Red tests for "A second machine can reach your PRISM" (task b064db4e).

Owner, 2026-07-30: a fresh install on machine B offered exactly ONE door,
"Welcome back. Claim this PRISM as yours" (ClaimPage.tsx:48-54), and behind it
a dashboard painting zeros. So the second machine invited the owner to claim a
SECOND, EMPTY PRISM rather than reach the one holding their 363 tasks, and the
empty install looked healthy while it did it. That ambiguity cost an hour.

Remote auth already shipped (task 4367c12f): enforce_team_boundary
(api/security.py:159-206) refuses an off-loopback caller without the owner key,
and KeyGatePage.tsx:13-38 signs in with it. What is missing is DISCOVERY and
HONESTY, not a sync engine and not a second credential path.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL TSX source - the documented convention at
tests/unit/test_dashboard_hydration_skeleton_ui.py:3-6 and
tests/unit/test_gate_banner_honest_state_ui.py:16-22. Per the repeat failures
those files record, these tests (a) STRIP comments before matching, so an
explanatory comment can never satisfy an assertion the way one did three times
on task 2ba63a22, (b) match the RENDERED TAG `<ConnectExistingPrism`, never the
bare name, and (c) parse the BALANCED `{...}` JSX expression after a marker
rather than a fixed character window.

They also pin the MOUNT POINT, not just the component: a control that exists
but is rendered nowhere is the e139295d failure (a correct SectionId with an
unreachable nav). AC-1 fails unless ClaimPage actually renders the tag.

RED TODAY: components/ConnectExistingPrism.tsx does not exist; ClaimPage's
pre-claim branch has exactly one button, "Claim this instance"; DashboardPage
has no empty-instance derivation and renders <Empty>No activity yet.</Empty>
(:244) plus "All indexes current." (:130) for a brand-new install; and
PRISM_VERSION is 7.10.0 with no v7.10.1 notes line.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "prism_service" / "web" / "src"
_CLAIM = _SRC / "pages" / "ClaimPage.tsx"
_DASH = _SRC / "pages" / "DashboardPage.tsx"
_CONNECT = _SRC / "components" / "ConnectExistingPrism.tsx"
_VERSION = _ROOT / "prism_service" / "__version__.py"

# The connect control is asserted as the RENDERED TAG, never the bare name -
# an import line or a comment mentioning it must not satisfy a mount assertion.
_MOUNTED = "<ConnectExistingPrism"

_LINE_COMMENT = re.compile(r"(?m)^[ \t]*//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _read(p: Path) -> str:
    assert p.exists(), (
        f"{p.name} does not exist yet - this slice creates it")
    return p.read_text(encoding="utf-8")


def _code(p: Path) -> str:
    """Source with comments removed.

    A comment explaining where a control lives satisfied three separate source
    assertions on task 2ba63a22. Only executable source may answer these tests.
    Block form also strips the JSX `{/* ... */}` body, leaving inert braces.
    Line comments are stripped only when they OWN the line, so a `https://`
    inside a string literal survives.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", _read(p)))


def _braced(src: str, marker: str) -> str:
    """The balanced `{...}` JSX expression beginning after ``marker``.

    Brace depth, never a fixed slice: a fixed window silently pushes the real
    guard out of view as soon as anything is inserted above it.
    """
    idx = src.find(marker)
    assert idx != -1, f"marker {marker!r} is absent - the AC is unmet or retarget the test"
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


def _pre_claim_branch(code: str) -> str:
    """The `key === null` arm of ClaimPage - the FIRST-RUN screen.

    Scoped deliberately: the post-claim arm ("You're the owner.") also renders
    buttons, so an unscoped match would pass on the wrong screen entirely.
    """
    start = code.find("key === null")
    assert start != -1, "ClaimPage's `key === null` first-run branch vanished"
    end = code.find("You're the owner.", start)
    assert end != -1, "ClaimPage's post-claim arm vanished - retarget the test"
    return code[start:end]


# ---------------------------------------------------------------------------
# AC-1 (R1) - the first-run screen offers BOTH doors, and connect is clickable
# ---------------------------------------------------------------------------

def test_ac1_first_run_screen_still_offers_claim():
    """The second door must be ADDED beside the first, never replace it. A
    machine that genuinely IS the owner's new primary still has to be claimed."""
    branch = _pre_claim_branch(_code(_CLAIM))
    assert "Claim this instance" in branch, (
        "AC-1: the existing claim affordance (ClaimPage.tsx:78-83) must "
        "survive - this slice adds a second door, it does not swap doors.")


def test_ac1_first_run_screen_mounts_the_connect_control():
    branch = _pre_claim_branch(_code(_CLAIM))
    assert _MOUNTED in branch, (
        "AC-1: the first-run screen must RENDER the connect affordance "
        f"({_MOUNTED} ...) inside the `key === null` branch, beside the claim "
        "button. Today it offers only 'Claim this instance', which invites the "
        "owner to claim a second, empty PRISM instead of reaching their own. "
        "Asserted as the rendered tag inside the first-run arm, because a "
        "component that exists but is mounted nowhere is unreachable (e139295d).")


def test_ac1_claim_page_really_imports_the_control():
    code = _code(_CLAIM)
    assert re.search(r"^\s*import\s+ConnectExistingPrism\s+from\s+", code, re.M), (
        "AC-1: ClaimPage must import ConnectExistingPrism - a JSX tag with no "
        "import does not compile, so the mount above would be a dead string.")


def test_ac1_connect_control_is_a_real_control_a_person_operates():
    """Pin the AFFORDANCE, not the constant behind it: an address field the
    person types into and a button they click."""
    code = _code(_CONNECT)
    assert re.search(r"<input\b[^>]*onChange", code, re.S), (
        "AC-1: the control needs an address INPUT the owner types machine A's "
        "url into.")
    assert re.search(r"<button\b[^>]*onClick", code, re.S), (
        "AC-1: the control needs a real <button onClick=...> - the entry point "
        "a person actually clicks, not a constant or a comment.")
    assert re.search(r"[Cc]onnect", code) and re.search(r"existing", code, re.I), (
        "AC-1: the copy must offer connecting to an EXISTING PRISM, so the "
        "owner can tell this door apart from 'claim this one'.")


def test_ac1_connect_navigates_this_browser_to_the_typed_address():
    """The whole design (plan OQ-1) is that connect = navigate to machine A,
    which reuses the shipped KeyGatePage sign-in. So the handler must really
    navigate, and to the TYPED value - not to a hardcoded url."""
    code = _code(_CONNECT)
    nav = re.search(r"window\.location\.(?:assign|replace)\s*\(|window\.location\.href\s*=",
                    code)
    assert nav, (
        "AC-1: clicking connect must navigate the browser to the given PRISM "
        "(window.location.assign/replace/href) - lib/api.ts:4,24 is same-origin "
        "by design, so there is no client-mode base url to point at instead.")
    tail = code[nav.end():nav.end() + 40].lstrip()
    assert not tail.startswith(('"', "'", "`")), (
        "AC-1: the navigation target must be the address the owner TYPED, not "
        "a string literal - a hardcoded destination is not a connect control.")


def _enclosing_braced(src: str, literal: str) -> str:
    """The balanced `{...}` expression that CONTAINS ``literal``.

    Scans backward to the matching open brace instead of slicing a window
    above the literal, so an inserted line cannot push the real guard out.
    """
    i = src.find(literal)
    assert i != -1, f"{literal!r} vanished from the source - retarget the test"
    depth = 0
    open_at = -1
    for j in range(i, -1, -1):
        if src[j] == "}":
            depth += 1
        elif src[j] == "{":
            if depth == 0:
                open_at = j
                break
            depth -= 1
    assert open_at != -1, f"{literal!r} is not inside a JSX expression"
    depth = 0
    for k in range(open_at, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:k + 1]
    raise AssertionError(f"unbalanced braces around {literal!r}")


# ---------------------------------------------------------------------------
# AC-4 (R4) - an empty, unconnected install SAYS it is new, not "0 / nothing"
# ---------------------------------------------------------------------------

_EMPTY_DECL = re.compile(r"const\s+(\w*(?:[Ee]mpty|[Nn]othing|[Ff]resh)\w*)\s*=")


def test_ac4_dashboard_derives_emptiness_from_the_hydrated_counts():
    code = _code(_DASH)
    for m in _EMPTY_DECL.finditer(code):
        if "kpis" in code[m.end():m.end() + 400]:
            return
    raise AssertionError(
        "AC-4: DashboardPage must derive a named emptiness flag from the "
        "HYDRATED kpis that api/dashboard.py:87-94 returns (brain_docs, "
        "entities, memories, tasks_active all 0). Today there is no such "
        "derivation, so a brand-new install cannot tell 'new and empty' from "
        "'your data is gone' and paints confident zeros either way.")


def test_ac4_hero_branches_on_the_empty_instance_not_just_no_activity_yet():
    code = _code(_DASH)
    hero = _braced(code, "Brain activity")
    assert _EMPTY_DECL.search(code), "AC-4 needs the emptiness flag (see above)"
    flag = _EMPTY_DECL.search(code).group(1)
    assert flag in hero, (
        "AC-4: the hero expression (DashboardPage.tsx:240-244) must branch on "
        f"`{flag}` so a hydrated-and-empty install renders the honest panel "
        "INSTEAD of <Empty>No activity yet.</Empty>, which reads as 'the work "
        "vanished'. It must still be gated behind hydration - not-fetched-yet "
        "is a third state, and the 89e90d1a skeletons stay.")
    assert "Loaded" in hero or "loaded" in hero, (
        "AC-4: the honest-empty branch must sit BEHIND the hydration flags "
        "(stateLoaded/actLoaded, :145-146) - claiming 'new and empty' before "
        "the first fetch settles is the same lie in a new costume.")


def test_ac4_the_honest_copy_distinguishes_new_from_lost():
    code = _code(_DASH)
    # Prose, not the <Empty> tag: `\bempty\b` alone matches `<Empty>` and
    # would false-pass on the very component whose zeros are the defect.
    assert re.search(r"new and empty|empty and new|new,? +(?:and +)?empty"
                     r"|nothing here yet|no data here yet", code, re.I), (
        "AC-4: the copy must say plainly that this install is NEW AND EMPTY.")
    assert re.search(r"nothing (?:is |was |has )?(?:missing|lost|gone|vanished)"
                     r"|no data (?:is|was|has been) (?:missing|lost|gone)"
                     r"|your data is (?:safe|still)", code, re.I), (
        "AC-4: it must also say the data is not LOST. 'new and empty' and "
        "'your work is gone' render identically today (0/0/0 + 'No activity "
        "yet.'); that ambiguity is the hour the owner lost.")


def test_ac4_the_empty_install_offers_the_way_to_reach_the_real_one():
    assert _MOUNTED in _code(_DASH), (
        "AC-4: an empty install that says 'you are new and empty' must also "
        f"offer the door out ({_MOUNTED} ...) - naming the problem without "
        "the remedy is the 401-with-no-remedy failure again.")


def test_ac4_all_indexes_current_is_not_claimed_by_an_unindexed_install():
    expr = _enclosing_braced(_code(_DASH), "All indexes current.")
    assert re.search(r"[Ee]mpty|[Nn]othing|[Nn]ever|[Ii]ndexed\b", expr), (
        "AC-4: StalenessCard (:129-131) tells a brand-new install 'All indexes "
        "current.' - technically true, and it reads as 'everything is fine, "
        "your data is simply gone'. The line must be conditioned on there "
        "being something indexed at all.")


# ---------------------------------------------------------------------------
# AC-6 (R6) / AC-7 (R7) - discovery must not widen exposure or fork auth
# ---------------------------------------------------------------------------

def test_ac7_remote_allowlist_does_not_grow_to_cover_project_data():
    from prism_service.api.security import _REMOTE_PUBLIC_PATHS

    assert set(_REMOTE_PUBLIC_PATHS) == {"/api/version", "/api/auth/mode"}, (
        "AC-7: shipping DISCOVERY must not widen EXPOSURE. The remote "
        "allowlist (api/security.py:86-89) stays the two boot routes the SPA "
        "needs to ask for a key. Anything added here is project data served to "
        "an unauthenticated caller - the recorded likely_misfire, which turns "
        "today's accidental hole into a documented feature.")


def test_ac6_the_connect_control_adds_no_second_credential_path():
    """R6: build ON the shipped door (lib/auth.ts, KeyGatePage.tsx), never a
    parallel one. Connect NAVIGATES; machine A's own KeyGatePage takes the key."""
    code = _code(_CONNECT)
    for smell in ("localStorage", "prism.accessKey", "password", "Bearer",
                  "authHeaders", "/api/auth/"):
        assert smell not in code, (
            f"AC-6: ConnectExistingPrism must not touch {smell!r} - handling a "
            "credential here forks authentication (stop_if #2). The control's "
            "whole job is to navigate; KeyGatePage.tsx:13-38 on the target "
            "instance already stores and PROVES the key.")


# ---------------------------------------------------------------------------
# AC-9 (R9) - the user-visible change carries its version bump
# ---------------------------------------------------------------------------

def test_ac9_version_patch_bumped_with_a_notes_line():
    """Pinned as the NOTES entry plus a semver FLOOR, never equality on the
    live PRISM_VERSION: that instrument rots on the very next patch bump and
    hands its red to the next lane (mx-dd2578, task 5a6837a0)."""
    src = _read(_VERSION)
    assert "v7.10.1:" in src, (
        "AC-9: PRISM_VERSION_NOTES needs a one-line v7.10.1 entry, in the SAME "
        "commit as the change (currently 7.10.0 at __version__.py:16).")
    m = re.search(r'PRISM_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, "PRISM_VERSION must be a parseable semver string"
    assert tuple(int(g) for g in m.groups()) >= (7, 10, 1), (
        "AC-9: PRISM_VERSION must be at least the 7.10.1 this slice bumps to.")
