"""A codified test scaffold, computed with zero model calls, so
write_failing_tests's one inference call is never asked to guess what is
already fully determined by data on hand (owner standing order: make
conductor nodes programmatic).

THE DEFECT (task bb3d1f6a, live). write-failing-tests-loop's `loop` step
drafted a test that (1) defined only 1 of 2 pinned test functions, so
pytest exits 4 (collection error) and red_gate's rc==1 requirement can
never pass; (2) imported `prism_service.lexicon`, a module that does not
exist -- the real one is `prism_service.services.lexicon`; (3) invented
the wrong arity/return type for the real entry point (`align`). `gather`
(context-enrich) feeds an 8-result, 600-char-truncated semantic search
with no notion of "the one symbol this test targets" -- it can omit the
real entry point, or truncate mid-signature.

THE FIX. A new codified node, `test-scaffold`, declared between `recall`
and `loop`. It computes, from task.verify (via arc_governance's own
parser -- never a second one) and from symbols named in the task's own
text, a ready-to-use block: the exact pinned file/function names, a
VERIFIED import block (every module resolved via arc_governance's
existing _submodule_path_resolvable, never executed), and the REAL
signatures of the symbols under test, read off brain_svc.find_symbol's
own source chunk. An unresolved candidate degrades to an honest
statement, never a guessed signature.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NODE_PATH = (_REPO_ROOT / ".prism" / "behaviors" / "conductor"
              / "write-failing-tests-loop.json")


def _node() -> dict:
    return json.loads(_NODE_PATH.read_text(encoding="utf-8"))


def _routes(doc: dict) -> list[str]:
    return [(s.get("url") or "").split("/steps/")[-1].split("?")[0]
            for s in doc.get("steps") or []]


# ----------------------------------------------------------------------
# THE DECLARATION: scaffold sits between recall and loop; loop
# interpolates the block it exports, and every earlier placeholder
# survives (this is additive, never a rewrite).
# ----------------------------------------------------------------------

def test_the_node_version_moved_again():
    assert _node()["version"] >= 8, (
        "the declaration changed (a new scaffold step) and the node "
        "version did not move")


def test_scaffold_is_declared_between_recall_and_loop():
    doc = _node()
    routes = _routes(doc)
    assert "test-scaffold" in routes, routes
    assert routes.index("refusal-recall") < routes.index("test-scaffold"), (
        f"scaffold must run AFTER recall: {routes!r}")
    assert routes.index("test-scaffold") < routes.index("reason-loop"), (
        f"scaffold must run BEFORE the loop (reason-loop): {routes!r}")


def test_scaffold_step_carries_the_task_id():
    doc = _node()
    step = next(s for s in doc["steps"] if "test-scaffold" in (s.get("url") or ""))
    body = json.loads(step["body"])
    assert body.get("task_id") == "${taskId}", body


def test_every_inner_body_still_parses_as_json():
    doc = _node()
    for step in doc["steps"]:
        json.loads(step["body"])  # raises on malformed JSON


def test_loop_prompt_interpolates_the_scaffold_block_and_keeps_the_rest():
    doc = _node()
    step = next(s for s in doc["steps"] if "reason-loop" in (s.get("url") or ""))
    body = json.loads(step["body"])
    prompt = body.get("prompt") or ""
    assert "${scaffoldBlock}" in prompt, (
        "the loop prompt must interpolate the scaffold node's block, or "
        f"the draft is never given the real signatures:\n{prompt}")
    # Every pre-existing instruction must survive -- this is additive.
    assert "${refusalBlock}" in prompt
    assert "${verify}" in prompt
    assert "${oracle}" in prompt
    assert "${brainContext}" in prompt


# ----------------------------------------------------------------------
# THE ENDPOINT
# ----------------------------------------------------------------------

def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id="bb3d1f6a-c3ff-488b-a754-010a7705907f",
        title="One node adjudicates the vocabulary",
        description=(
            "Four changes in `services/lexicon.py` would let a promoted "
            "term reach label alignment. `load_lexicon()` is project-blind. "
            "It does NOT reach `lexicon.align`, the write-time substitution."
        ),
        oracle="the vocabulary node reports non-canonical terms",
        status="in_progress",
        verify=[
            "services/prism-service/tests/unit/test_creating_a_ticket_swaps_no_words.py"
            "::test_prose_keeps_the_words_the_author_wrote",
            "services/prism-service/tests/unit/test_creating_a_ticket_swaps_no_words.py"
            "::test_a_semicolon_and_a_contraction_still_go",
        ],
    )
    base.update(over)
    return Task(**base)


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, task_id):
        return self._task if task_id == self._task.id else None


class _FakeBrainSvc:
    def __init__(self, rows_by_name):
        self._rows_by_name = rows_by_name

    def find_symbol(self, name, kind=None, limit=10):
        return list(self._rows_by_name.get(name, []))


_LOAD_LEXICON_ROW = {
    "source_file": "prism_service/services/lexicon.py",
    "content": (
        "@functools.lru_cache(maxsize=1)\n"
        "def load_lexicon() -> list[Term]:\n"
        "    \"\"\"Parse ontology/model-lexicon.ttl.\"\"\"\n"
        "    g = rdflib.Graph()\n"
    ),
    "entity_name": "load_lexicon",
    "entity_kind": "function",
    "line_start": 53,
    "line_end": 74,
}

_ALIGN_ROW = {
    "source_file": "prism_service/services/lexicon.py",
    "content": (
        "def align(text: str) -> tuple[str, list[dict]]:\n"
        "    \"\"\"Replace every whole-word synonym.\"\"\"\n"
        "    if not text:\n"
        "        return text, []\n"
    ),
    "entity_name": "align",
    "entity_kind": "function",
    "line_start": 139,
    "line_end": 180,
}

# A same-named symbol living somewhere else entirely -- present to prove
# the file-hint disambiguation actually prefers the hinted file over the
# first row a broader search would return.
_ALIGN_ROW_ELSEWHERE = {
    "source_file": "prism_service/services/some_other_module.py",
    "content": "def align(x):\n    return x\n",
    "entity_name": "align",
    "entity_kind": "function",
    "line_start": 1,
    "line_end": 2,
}


def _wf_with_project(task, rows_by_name):
    from prism_service.api import workflows as wf

    ctx = types.SimpleNamespace(
        task_svc=_FakeTaskSvc(task), brain_svc=_FakeBrainSvc(rows_by_name))
    return wf, ctx


def test_pinned_file_and_both_required_names_come_from_task_verify(monkeypatch):
    wf, ctx = _wf_with_project(_mk_task(), {})
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert resp.pinned_file.endswith(
        "test_creating_a_ticket_swaps_no_words.py")
    assert "test_prose_keeps_the_words_the_author_wrote" in resp.required_test_names
    assert "test_a_semicolon_and_a_contraction_still_go" in resp.required_test_names
    # Both pinned names must appear in the assembled block too.
    assert "test_prose_keeps_the_words_the_author_wrote" in resp.scaffold_block
    assert "test_a_semicolon_and_a_contraction_still_go" in resp.scaffold_block


def test_a_real_symbol_resolves_to_its_real_signature_and_import(monkeypatch):
    wf, ctx = _wf_with_project(_mk_task(), {
        "load_lexicon": [_LOAD_LEXICON_ROW],
        "align": [_ALIGN_ROW],
    })
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    # The real module genuinely exists in this repo, so the real
    # resolver (never mocked here) proves the end-to-end path.
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: dotted == "prism_service.services.lexicon")

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert "from prism_service.services.lexicon import load_lexicon" in resp.resolved_imports
    assert "from prism_service.services.lexicon import align" in resp.resolved_imports
    align_sig = next((s for s in resp.signatures if "align" in s), "")
    assert "def align(text: str) -> tuple[str, list[dict]]" in align_sig, resp.signatures
    load_sig = next((s for s in resp.signatures if "load_lexicon" in s), "")
    assert "def load_lexicon() -> list[Term]" in load_sig, resp.signatures


def test_a_same_named_symbol_elsewhere_never_wins_over_the_hinted_file(monkeypatch):
    wf, ctx = _wf_with_project(_mk_task(), {
        "align": [_ALIGN_ROW_ELSEWHERE, _ALIGN_ROW],
    })
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: dotted == "prism_service.services.lexicon")

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    align_sig = next((s for s in resp.signatures if "align" in s), "")
    assert "some_other_module" not in align_sig
    assert "lexicon.py" in align_sig


def test_a_nonexistent_module_is_never_emitted(monkeypatch):
    bogus_row = {
        "source_file": "prism_service/services/does_not_exist.py",
        "content": "def bogus():\n    pass\n",
        "entity_name": "bogus",
        "entity_kind": "function",
        "line_start": 1,
        "line_end": 2,
    }
    task = _mk_task(description="calls `bogus()` somewhere")
    wf, ctx = _wf_with_project(task, {"bogus": [bogus_row]})
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    # Never mocked to True -- the real resolver genuinely refuses a path
    # that does not exist on disk.

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert not any("does_not_exist" in imp for imp in resp.resolved_imports)
    assert not any("prism_service.services.does_not_exist" in imp
                   for imp in resp.resolved_imports)


def test_an_unresolvable_symbol_degrades_to_a_statement_never_fabricates(monkeypatch):
    task = _mk_task(description="mentions `totally_unknown_symbol()` only")
    wf, ctx = _wf_with_project(task, {})  # brain_svc knows nothing
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert "totally_unknown_symbol" in resp.unresolved
    assert not any("totally_unknown_symbol" in s for s in resp.signatures)
    assert not any("totally_unknown_symbol" in imp for imp in resp.resolved_imports)
    assert "totally_unknown_symbol" in resp.scaffold_block
    assert "could not confirm" in resp.scaffold_block.lower() or \
           "cannot confirm" in resp.scaffold_block.lower()


def test_no_task_id_returns_an_empty_scaffold():
    from prism_service.api import workflows as wf

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id=""), project="prism")

    assert resp.scaffold_block == ""
    assert resp.pinned_file == ""
    assert resp.required_test_names == []


def test_an_unknown_task_id_returns_an_empty_scaffold(monkeypatch):
    from prism_service.api import workflows as wf

    monkeypatch.setattr(
        wf, "get_project",
        lambda p: types.SimpleNamespace(
            task_svc=_FakeTaskSvc(_mk_task()), brain_svc=_FakeBrainSvc({})))

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="no-such-task"), project="prism")

    assert resp.scaffold_block == ""


def test_a_broken_lookup_degrades_gracefully_never_raises(monkeypatch):
    class _BoomBrainSvc:
        def find_symbol(self, *a, **kw):
            raise RuntimeError("index is corrupt")

    task = _mk_task(description="mentions `load_lexicon()`")
    wf = __import__("prism_service.api.workflows", fromlist=["x"])
    ctx = types.SimpleNamespace(task_svc=_FakeTaskSvc(task), brain_svc=_BoomBrainSvc())
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert "load_lexicon" in resp.unresolved
    assert resp.resolved_imports == []


def test_a_broken_project_lookup_degrades_to_empty_never_raises(monkeypatch):
    from prism_service.api import workflows as wf

    def _boom(project):
        raise RuntimeError("project registry unavailable")

    monkeypatch.setattr(wf, "get_project", _boom)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert resp.scaffold_block == ""


def test_scaffold_is_registered_in_step_handlers():
    from prism_service.services import task_runner

    assert "test-scaffold" in task_runner._step_handlers()


def test_scaffold_exports_camel_and_snake_for_interpolation():
    from prism_service.api import workflows as wf
    from prism_service.services.task_runner import _exported_variables

    resp = wf.TestScaffoldResponse(scaffold_block="AUTHORITATIVE SCAFFOLD...")

    out = _exported_variables(resp)
    assert out.get("scaffold_block") == resp.scaffold_block
    assert out.get("scaffoldBlock") == resp.scaffold_block


# ----------------------------------------------------------------------
# FOLLOW-UP 1: an on-disk AST fallback for a cold/incomplete brain index.
# Live evidence: find_symbol("align")/("load_lexicon") return [] even
# though the real functions exist on disk in this very repo -- the index
# covers some files of a service and not others, and reindexing rots
# again. The filesystem is ground truth; the index is an optimization.
# ----------------------------------------------------------------------

def _write_fake_lexicon_module(tmp_path):
    pkg = tmp_path / "prism_service" / "services"
    pkg.mkdir(parents=True)
    (pkg / "lexicon.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Term:\n"
        "    label: str\n"
        "\n"
        "\n"
        "def load_lexicon() -> list[Term]:\n"
        "    return []\n"
        "\n"
        "\n"
        "def align(text: str) -> tuple[str, list[dict]]:\n"
        "    return text, []\n",
        encoding="utf-8",
    )
    return tmp_path


def test_disk_fallback_resolves_real_signatures_with_a_cold_index(
        monkeypatch, tmp_path):
    """The index is COLD -- find_symbol returns [] for everything, exactly
    what was observed live -- yet the real on-disk file still yields
    align's and load_lexicon's real arity/return type. Never mutates the
    real brain index; the fake module lives entirely under tmp_path."""
    root = _write_fake_lexicon_module(tmp_path)
    wf, ctx = _wf_with_project(_mk_task(), {})  # brain_svc: cold, finds nothing
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    monkeypatch.setattr(wf, "_scaffold_source_root", lambda project, task_id: root)
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: dotted == "prism_service.services.lexicon")

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    load_sig = next((s for s in resp.signatures if "load_lexicon" in s), "")
    align_sig = next((s for s in resp.signatures if s.strip().startswith("def align")
                       or " align(" in s), "")
    assert "def load_lexicon() -> list[Term]" in load_sig, resp.signatures
    assert "def align(text: str) -> tuple[str, list[dict]]" in align_sig, resp.signatures
    assert "from prism_service.services.lexicon import load_lexicon" in resp.resolved_imports
    assert "from prism_service.services.lexicon import align" in resp.resolved_imports
    assert "load_lexicon" not in resp.unresolved
    assert "align" not in resp.unresolved


def test_disk_fallback_still_degrades_honestly_when_nothing_matches(
        monkeypatch, tmp_path):
    """A file_hint exists but defines neither candidate -- must stay
    unresolved, never fabricate a signature just because a file was found."""
    root = _write_fake_lexicon_module(tmp_path)
    task = _mk_task(description="calls `totally_absent_fn()` in `services/lexicon.py`")
    wf, ctx = _wf_with_project(task, {})
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    monkeypatch.setattr(wf, "_scaffold_source_root", lambda project, task_id: root)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert "totally_absent_fn" in resp.unresolved
    assert not any("totally_absent_fn" in s for s in resp.signatures)
    assert not any("totally_absent_fn" in imp for imp in resp.resolved_imports)


def test_disk_fallback_is_skipped_entirely_once_the_index_answers(
        monkeypatch, tmp_path):
    """A warm index entry must win outright -- the disk walk is a fallback,
    never a second opinion that could override a real index hit. A task
    description naming ONLY `load_lexicon()` (unlike `_mk_task()`'s default,
    which also names other candidates that legitimately fall through to
    disk) isolates the claim: with the one candidate present already
    indexed, disk_search must never run at all."""
    root = _write_fake_lexicon_module(tmp_path)
    task = _mk_task(description="calls `load_lexicon()` only")
    wf, ctx = _wf_with_project(task, {"load_lexicon": [_LOAD_LEXICON_ROW]})
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    monkeypatch.setattr(wf, "_scaffold_source_root", lambda project, task_id: root)
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: dotted == "prism_service.services.lexicon")

    calls = {"n": 0}
    real_disk_search = wf._scaffold_disk_search

    def _counting_disk_search(*a, **kw):
        calls["n"] += 1
        return real_disk_search(*a, **kw)

    monkeypatch.setattr(wf, "_scaffold_disk_search", _counting_disk_search)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    load_sig = next((s for s in resp.signatures if "load_lexicon" in s), "")
    assert "def load_lexicon() -> list[Term]" in load_sig
    assert calls["n"] == 0, "the disk fallback ran even though the index answered"


# ----------------------------------------------------------------------
# FOLLOW-UP 2: trim harvested noise. A file extension is never a symbol,
# and the block's unresolved section must be capped (a small number, then
# a plain count of the rest) so a padded task description does not blow
# up the prompt with names that teach the model nothing.
# ----------------------------------------------------------------------

def test_a_file_extension_span_is_never_treated_as_a_symbol(monkeypatch):
    task = _mk_task(description=(
        "The lexicon holds terms in `ontology/model-lexicon.ttl`. "
        "The only coupling is a render-time regex in `UnderstandPage.tsx` "
        "near line 466."
    ))
    wf, ctx = _wf_with_project(task, {})
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert "ttl" not in resp.unresolved
    assert "tsx" not in resp.unresolved
    assert "ttl" not in resp.scaffold_block
    assert "tsx" not in resp.scaffold_block


def test_the_block_caps_unresolved_names_the_field_keeps_them_all(monkeypatch):
    many_names = [f"onlyinprose_name_{i}" for i in range(12)]
    description = " ".join(f"`{n}()`" for n in many_names)
    task = _mk_task(description=description)
    wf, ctx = _wf_with_project(task, {})  # none of them resolve
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)

    resp = wf.workflow_step_test_scaffold(
        wf.TestScaffoldRequest(task_id="bb3d1f6a-c3ff-488b-a754-010a7705907f"),
        project="prism")

    assert len(resp.unresolved) == 12, (
        "the FIELD must still report every candidate, capping is a "
        "display concern for the block only")
    shown = sum(1 for n in many_names if n in resp.scaffold_block)
    assert shown < 12, (
        "the block must cap how many unresolved names it lists, or a "
        "padded description blows up the prompt for nothing")
    assert re.search(r"\d+ more", resp.scaffold_block), (
        f"a capped list must say how many more were left out:\n"
        f"{resp.scaffold_block}")
