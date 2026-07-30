"""RED scaffold — an unwired slice cannot be authored (task 597839d9).

Epic 0784729f shipped five machine-green slices whose assembled feature could
not run: no adapter was ever registered in production (EVERY test injected
one), the surface was invisible in local mode, Start was inert. The gates
proved exactly what they were asked to prove — the defect was in how the tasks
were AUTHORED (mx-8f4666). Prose will not hold; the constraint belongs in the
validator that already blocks creation.

Two new checks, plus explicit guards that they never refuse honest work: a
validator drivers route around is worse than no validator.

Imports live INSIDE the tests so the file collects and fails at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PKG = "services/prism-service/prism_service"

# Real files that exist today — used as the "existing production caller".
EXISTING_CALLER = f"{_PKG}/api/tasks.py"
EXISTING_SERVICE = f"{_PKG}/services/task_service.py"
# A module that does not exist — the unwired new thing.
NEW_MODULE = f"{_PKG}/services/some_brand_new_adapter.py"
NEW_TEST = "services/prism-service/tests/unit/test_some_brand_new_adapter.py"

GOOD_ORACLE = ("A user opens http://127.0.0.1:8888/tasks and sees the imported "
               "row; focused tests pin the identity rules.")
TEST_ONLY_ORACLE = ("Focused tests prove two connections may use the same "
                    "display key without collision, and repeated exact-ID "
                    "imports update one link.")


def _validate(**kw):
    from prism_service.services.oracle_authoring import validate_for_authoring
    return validate_for_authoring(**kw)


# ── AC-1 / AC-2: the wiring check ──────────────────────────────────────

def test_a_new_module_with_no_existing_caller_is_refused():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="demo",
        allowed_files=[NEW_MODULE, NEW_TEST])
    assert errors, "a slice nothing constructs must be refused at authoring time"
    assert "some_brand_new_adapter" in " ".join(errors), (
        "the error must NAME the unwired module so the author can repair it")


def test_the_same_slice_passes_once_a_real_caller_is_included():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="demo",
        allowed_files=[NEW_MODULE, EXISTING_CALLER, NEW_TEST])
    assert errors == [], (
        f"including an existing production caller must satisfy the check; got "
        f"{errors}")


# ── AC-3 / AC-4: the oracle-surface check ─────────────────────────────

def test_a_pure_test_vocabulary_oracle_is_refused():
    """This is the REAL oracle from fddfd75a, the slice that shipped dead."""
    _spec, errors = _validate(
        oracle=TEST_ONLY_ORACLE, proof_type="demo",
        allowed_files=[EXISTING_SERVICE])
    assert errors, (
        "an oracle satisfiable with everything mocked is a verify command, "
        "not an oracle")
    assert "surface" in " ".join(errors).lower()


def test_an_oracle_naming_a_surface_passes():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="demo",
        allowed_files=[EXISTING_SERVICE])
    assert errors == [], f"tests PLUS a surface is the target shape; got {errors}"


# ── AC-5: the misfire guards — honest work is never refused ───────────

def test_a_pure_refactor_is_not_refused():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="demo",
        allowed_files=[EXISTING_CALLER, EXISTING_SERVICE])
    assert errors == [], f"a refactor of existing files must pass; got {errors}"


def test_a_tests_only_slice_is_not_refused():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="demo", allowed_files=[NEW_TEST])
    assert errors == [], f"a tests-only slice must pass; got {errors}"


def test_an_empty_file_list_is_not_refused():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="demo", allowed_files=[])
    assert errors == [], f"no file list means nothing to check; got {errors}"


def test_omitting_allowed_files_entirely_still_works():
    """Existing callers must keep working — the parameter is optional."""
    _spec, errors = _validate(oracle=GOOD_ORACLE, proof_type="demo")
    assert errors == []


# ── AC-7: the pre-existing check survives ─────────────────────────────

def test_the_existing_pytest_ids_check_still_fires():
    _spec, errors = _validate(
        oracle=GOOD_ORACLE, proof_type="test", verify=["echo nope"],
        allowed_files=[EXISTING_SERVICE])
    assert any("pytest node" in e for e in errors), (
        f"the original proof_type=test check must still reject; got {errors}")


# ── AC-6: BOTH authoring paths enforce it ─────────────────────────────

def test_both_callers_forward_allowed_files():
    """If only one path enforces, the other is the way around it."""
    rest = (_SERVICE_ROOT / "prism_service" / "api" / "tasks.py").read_text(
        encoding="utf-8")
    mcp = (_SERVICE_ROOT / "prism_service" / "mcp" / "tools.py").read_text(
        encoding="utf-8")
    def _call_text(src: str) -> str:
        """The ACTUAL call, delimited by paren balance — not a fixed window,
        which a comment inside the call can push the argument out of."""
        i = src.index("validate_for_authoring(")
        start = src.index("(", i)
        depth = 0
        for j in range(start, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    return src[start:j + 1]
        raise AssertionError("unbalanced validate_for_authoring( call")

    for name, src in (("api/tasks.py", rest), ("mcp/tools.py", mcp)):
        assert "allowed_files" in _call_text(src), (
            f"{name} calls validate_for_authoring without passing "
            "allowed_files — that path still accepts unwired tasks")
