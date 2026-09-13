"""UI contract test: opening Settings must not fan out one request per project.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL web source (TSX) -- the same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py.

MEASURED DEFECT (2026-09-13, live on the AOS dev instance): loading /settings
issued 154 sequential `GET /api/understand?project=<name>` calls, one for every
project the instance has ever seen (gac-*, cwl-*, beatrank-*, throwaway fixture
projects like `doesnotexist12345`). `SettingsPage` fired `loadAllInfos(projects)`
from a mount effect regardless of which section was on screen, even though the
resulting `infos` map is read ONLY by the "projects" section's cards. So merely
opening Settings > Access key -- the page a user must reach to enable Remote
assist -- stampeded the daemon and the UI read as unresponsive.

The guard asserted here is BEHAVIOURAL, not cosmetic: the effect that calls
`loadAllInfos` must be gated on the active section, and must re-run when that
section changes (otherwise arriving at the projects tab would render empty
cards forever).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_SETTINGS = _SRC / "pages" / "SettingsPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _effect_calling(src: str, call: str) -> str:
    """Return the useEffect(...) block that CALLS `call`.

    Deliberately anchored on the real call site and walked back to the
    enclosing `useEffect(` -- never a fixed character window, so a comment
    sitting above the effect cannot satisfy an assertion about its body
    (the trap recorded in CLAUDE.md's lessons).
    """
    # Strip line comments first: a comment must never be able to satisfy
    # any assertion below.
    code = re.sub(r"//[^\n]*", "", src)
    idx = code.index(f"{call}(projects)")
    start = code.rindex("useEffect(", 0, idx)
    end = code.index(");", code.index("}, [", idx))
    return code[start:end + 2]


def test_settings_only_loads_project_infos_on_the_projects_section():
    """The whole point: no per-project fan-out unless the cards are shown."""
    block = _effect_calling(_read(_SETTINGS), "loadAllInfos")
    assert 'section !== "projects"' in block, (
        "the loadAllInfos effect must bail out unless the projects section is "
        "on screen; without this guard every Settings visit issues one "
        "/api/understand request per project"
    )


def test_the_project_info_effect_reruns_when_the_section_changes():
    """A guard with a stale dep list would leave the cards permanently empty."""
    block = _effect_calling(_read(_SETTINGS), "loadAllInfos")
    deps = block[block.rindex("}, ["):]
    assert "section" in deps, (
        f"`section` must be in the effect's dependency array, got: {deps!r}"
    )


def test_project_infos_are_read_only_by_the_projects_section():
    """Pins the premise of the guard.

    If a future section starts reading `infos`, this test fails and whoever
    added it has to decide how that section gets its data -- rather than
    silently reintroducing the mount-time stampede.
    """
    src = re.sub(r"//[^\n]*", "", _read(_SETTINGS))
    readers = [m.start() for m in re.finditer(r"\binfos\[", src)]
    assert readers, "expected at least one `infos[...]` read to anchor on"
    projects_section = src.index('section === "projects"')
    next_section = src.index("section === ", projects_section + 10)
    for pos in readers:
        assert projects_section < pos < next_section, (
            "an `infos[...]` read escaped the projects section; the fan-out "
            "guard in the loadAllInfos effect would starve it"
        )
