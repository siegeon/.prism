"""Task d1854966: the Inbox nav entry and route are HIDDEN behind a single
INBOX_ENABLED flag while the feature is under development. This is a HIDE,
not a delete and not the rollback PR #322 owns separately: the item literal,
the /inbox route registration, and the InboxPage lazy import all remain in
source, reversible with a one-line flip of the flag.

Convention (test_conductor_page_animated_cleanup_ui.py): the PRISM SPA has no
JS test runner, so UI ACs are pinned by asserting the ACTUAL TSX source. All
assertions here strip comments first and parse the enclosing structure (never
a fixed character window around a match) — an explanatory comment above the
element must not satisfy an assertion (lesson: e139295d).

FR-2 correction recorded in plan_doc: the gate must NOT reuse
version.dev_mode — the 8888 dev daemon (the only instance the owner looks
at) reports dev_mode:true, which would leave Inbox visible there. The gate is
exactly one exported module-level constant, INBOX_ENABLED, declared in
Sidebar.tsx and shared by both the nav guard and the App.tsx route guard.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _strip_comments(src: str) -> str:
    # Strip // line comments FIRST, then /* */ block comments — some line
    # comments in this file contain a literal "/*" substring (e.g. the
    # "// /settings/*." path-wildcard comment), which a block-comment pass
    # run first would misread as a real block-comment opener and eat
    # everything up to the next "*/" anywhere in the file. Stripping line
    # comments first removes those false openers along with their line.
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _sidebar() -> str:
    return _strip_comments((_WEB / "components" / "Sidebar.tsx").read_text(encoding="utf-8"))


def _app() -> str:
    return _strip_comments((_WEB / "App.tsx").read_text(encoding="utf-8"))


def _activity_items() -> str:
    src = _sidebar()
    m = re.search(r'label:\s*"Activity".*?items:\s*\[(.*?)\n\s*\],', src, re.DOTALL)
    assert m, "could not locate the Activity section's items array in Sidebar.tsx"
    return m.group(1)


def test_inbox_enabled_flag_exists_and_is_false():
    """Exactly ONE named flag gates both surfaces, exported so App.tsx can
    import it, declared false by default. Not two independent conditionals,
    not a re-use of version.dev_mode (verified wrong — see module docstring)."""
    src = _sidebar()
    m = re.search(r'\bexport\s+const\s+INBOX_ENABLED\s*=\s*(true|false)\s*;', src)
    assert m, (
        "INBOX_ENABLED is not declared as an exported module-level const "
        "in Sidebar.tsx")
    assert m.group(1) == "false", (
        "INBOX_ENABLED must default to false while the feature is hidden")


def test_the_inbox_item_literal_still_exists_in_source():
    """A hide, not a delete: the item's literal definition (to/label/icon/
    isNew) stays intact and reversible with a one-line edit."""
    src = _sidebar()
    item = re.search(
        r'\{[^{}]*to:\s*"/inbox"[^{}]*label:\s*"Inbox"[^{}]*icon:\s*Inbox'
        r'[^{}]*isNew:\s*true[^{}]*\}',
        src,
    )
    assert item, "the Inbox nav item literal must remain intact in Sidebar.tsx"


def test_the_inbox_item_is_gated_by_inbox_enabled_in_the_items_array():
    """The item must sit inside an INBOX_ENABLED-guarded conditional spread
    in the Activity items array (existing precedent: Sidebar.tsx's own
    `...(inSettings ? SETTINGS_SECTIONS : MAIN_SECTIONS)` spread) — not a
    bare array member, or there is nothing for dead-branch elimination to
    strip and the item always renders."""
    items_src = _activity_items()
    guard = re.search(
        r'\.\.\.\(?\s*INBOX_ENABLED\s*\?\s*\[\s*\{[^{}]*to:\s*"/inbox"',
        items_src,
    )
    assert guard, (
        "the /inbox item is not wrapped in an INBOX_ENABLED-guarded "
        f"conditional spread inside Activity.items: {items_src!r}")


def test_sibling_activity_items_stay_in_order():
    """No sibling Activity item is reordered by the guard — Work, Conductor,
    Live, Retrievals stay in their original relative order."""
    items_src = _activity_items()
    positions = [
        m.start()
        for m in re.finditer(r'to:\s*"(/tasks|/conductor|/live|/retrievals)"', items_src)
    ]
    assert len(positions) == 4, f"expected all 4 siblings present: {items_src!r}"
    assert positions == sorted(positions), "Activity siblings must stay in original order"


def _inbox_route_element() -> str:
    src = _app()
    route = re.search(r'<Route\s+path="/inbox"\s+element=\{([^}]*)\}\s*/>', src)
    assert route, 'no <Route path="/inbox"> element found in App.tsx'
    return route.group(1)


def test_route_element_is_gated_by_inbox_enabled():
    """A typed/bookmarked /inbox URL must not mount InboxPage while the flag
    is off — the falsy branch redirects to Dashboard (existing pattern:
    App.tsx's own /explore and /graph -> /brain redirects)."""
    element_expr = _inbox_route_element()
    assert "INBOX_ENABLED" in element_expr, (
        f"the /inbox route element is not gated by INBOX_ENABLED: {element_expr!r}")
    assert re.search(r'<Navigate\s+to="/"\s+replace\s*/>', element_expr), (
        f"the /inbox route's falsy branch must land on Dashboard: {element_expr!r}")
    assert "InboxPage" in element_expr, (
        f"the /inbox route's truthy branch must still mount InboxPage: {element_expr!r}")


def test_route_gate_is_not_dev_mode():
    """Must not reuse version.dev_mode — verified wrong (module docstring):
    the 8888 dev daemon reports dev_mode:true, which would leave Inbox
    reachable on the only instance the owner looks at."""
    element_expr = _inbox_route_element()
    assert "dev_mode" not in element_expr, (
        "the /inbox route must not gate on version.dev_mode")


def test_inbox_page_lazy_import_still_exists():
    """Reversible gate: InboxPage's lazy import (App.tsx:23) stays in
    source, untouched by the route guard."""
    src = _app()
    assert re.search(
        r'const\s+InboxPage\s*=\s*lazyRoute\("inbox",\s*\(\)\s*=>\s*import\("@/pages/InboxPage"\)\);',
        src,
    ), "InboxPage's recoverable lazy import must remain in App.tsx"


def test_app_imports_inbox_enabled_from_sidebar():
    """The route guard must consume the SAME flag the nav guard consumes,
    not a locally re-declared copy — imported from Sidebar.tsx, its one
    declaration site."""
    src = _app()
    assert re.search(r'INBOX_ENABLED', src) and re.search(
        r'import\s+.*INBOX_ENABLED.*from\s+"@/components/Sidebar"', src
    ), "App.tsx must import INBOX_ENABLED from @/components/Sidebar, not redeclare it"
