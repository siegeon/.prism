"""Explore must keep the archify embed mounted through a node click, and
must add doors to the delta view and the code/concepts/language lens.

The convention here (no JS test runner in this SPA) is to assert the ACTUAL
TSX source. Every assertion below matches a RENDERED TAG or a literal a
browser acts on, never a comment: a comment naming a component has satisfied
this kind of check before and hid a surface nobody could open.

Task dc1232dc-44cf-4b10-b56d-0a9e2622da1f, child of epic e89f5ff3. Before
this slice, StartHere.onMapNode turned any node click into onPick(target),
which set ?focus= and swapped <ArchifyMaps> for <Mesh> on the very first
click -- the artifact unmounted before the reader ever touched its own
Semantic Passport, Route Probe, Semantic Lens, chapter rail or export menu.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
_EXPLORE = _WEB / "pages" / "ExplorePage.tsx"


def _src() -> str:
    return _EXPLORE.read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """Strip `//` line comments before scanning for a code pattern -- this
    page's own top-of-file comment narrates the bug being fixed and must
    never be able to satisfy a "the old thing is gone" assertion. ExplorePage
    has no `://` inside a string literal, so a naive per-line strip is safe
    here."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _function_body(src: str, const_name: str) -> str:
    """Slice the RHS of `const <const_name> = (...) => { ... };` by brace
    matching, never a fixed character window -- a fixed window can cut a
    function short or bleed into the next statement."""
    m = re.search(rf"const {re.escape(const_name)} = [^{{]*\{{", src)
    assert m, f"could not find `const {const_name} = ... {{` in ExplorePage.tsx"
    depth = 1
    i = m.end()
    while depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[m.start():i]


def test_the_artifact_is_still_mounted():
    src = _src()
    assert "<ArchifyMaps" in src
    assert 'from "@/components/maps/ArchifyMaps"' in src


def test_a_node_click_no_longer_sets_focus_directly():
    """The old bug: onMapNode called onPick(target), which called
    setFocus(token), swapping <ArchifyMaps> out for <Mesh> on the first
    click. onPick must be gone entirely, and the click handler that
    ArchifyMaps invokes must record a pick, never touch ?focus= itself."""
    src = _src()
    code = _code_only(src)
    assert "onPick" not in code, (
        "onPick must be gone from the real code -- it is what turned a "
        "node click into an immediate ?focus= navigation that unmounted "
        "the artifact")

    body = _function_body(src, "onMapNode")
    assert "setFocus(" not in body, (
        f"onMapNode must not call setFocus directly, it only records the "
        f"pick: {body}"
    )
    assert "setPicked(" in body, (
        f"onMapNode must record the click as a pick for the explicit mesh "
        f"door to use: {body}"
    )
    assert 'onNodeSelect={onMapNode}' in src, (
        "ArchifyMaps must route clicks through onMapNode")


def test_an_explicit_mesh_door_exists_and_is_a_separate_click():
    """A small, clearly-labeled control opens the mesh -- the click that
    used to be automatic is now the reader's own choice."""
    src = _src()
    assert "const openPickedInMesh = () => {" in src
    door_body = _function_body(src, "openPickedInMesh")
    assert "setFocus(picked.target)" in door_body, (
        "the mesh door itself, not the node click, is what navigates")
    assert 'onClick={openPickedInMesh}' in src, (
        "a rendered control must call the mesh door handler")
    assert "in mesh" in src, (
        "the door's label must clearly say what clicking it does")


def test_delta_control_exists_and_is_conditioned_on_availability():
    src = _src()
    assert "ArchifyView" in src, "the page must know about the delta view"
    assert "const [view, setView] = useState<ArchifyView>(\"map\");" in src
    assert "const [deltaAvailable, setDeltaAvailable] = useState(false);" in src
    assert "onDeltaAvailability={setDeltaAvailable}" in src, (
        "the embed must be given a way to report whether the delta exists")
    assert 'view === "delta" ? "map" : "delta"' in src, (
        "a control must toggle the embed between the map and the delta")
    assert "disabled={!deltaAvailable}" in src, (
        "the delta control must be conditioned on availability, not always "
        "clickable -- the delta 404s until a task land has replaced a map")


def test_lens_strip_reaches_concepts_and_language():
    src = _src()
    assert '{ kind: "code", label: "Code" }' in src
    assert '{ kind: "concepts", label: "Concepts" }' in src
    assert '{ kind: "language", label: "Language" }' in src
    assert "LENS.map((t) =>" in src, (
        "the lens list must actually be rendered as controls")
    assert "setLens(t.kind)" in src, (
        "clicking a lens entry must switch which map the mounted embed "
        "draws")
    # And the embed itself must be driven by that state, not a second fixed
    # kind sitting beside it.
    assert "kind={lens}" in src


def test_inbound_focus_param_still_opens_the_mesh_directly():
    """Other pages (the /live graph's explore hop, ?session=/?task=
    normalisation) link to /brain?focus=<token> expecting the mesh, not the
    artifact. Only the CLICK behaviour changed, never this contract."""
    src = _src()
    assert "<Mesh token={focus} project={project} hops={hops} onFocus={setFocus}" in src, (
        "an inbound ?focus= must still render <Mesh>, centred on that token")
    assert 'p.get("session") || p.get("task")' in src
    assert "setFocus(paramFocusSeed, { replace: true })" in src
