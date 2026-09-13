"""A typed pydantic step result must export its scalar fields too.

WHAT WAS WRONG. `_exported_variables` only read a plain `dict` result or a
`result.reason` dict (task_runner.py:459). `/api/workflows/steps/
context-enrich` answers with `StepEnrichResponse`, a pydantic model with no
`.reason` attribute -- so a `gather` step wired to that route exported
NOTHING, and a later step's `${brainContext}` placeholder would reach the
model unfilled. This pins the fix: read the model's own scalar fields
(model_dump()/dict()/__dict__) the same way a plain dict result already
is, keeping the existing snake_case+camelCase double export and the
existing scalar-only filter (a non-scalar field, e.g. a list, must still
be dropped).
"""

from __future__ import annotations

from prism_service.services import task_runner


class _FakeTypedResult:
    """Stands in for a pydantic BaseModel without requiring one here."""

    def __init__(self, **fields):
        self._fields = fields

    def model_dump(self):
        return dict(self._fields)


def test_a_typed_result_with_no_reason_attribute_still_exports_scalars():
    result = _FakeTypedResult(brain_context="repo uses prism_service, not "
                                             "prism", relevant_memory=["x"])
    out = task_runner._exported_variables(result)
    assert out.get("brain_context") == (
        "repo uses prism_service, not prism"), (
        f"a typed step result's scalar field was not exported: {out!r}")
    assert out.get("brainContext") == (
        "repo uses prism_service, not prism"), (
        "the camelCase spelling must be exported too, same as a dict result")
    assert "relevant_memory" not in out and "relevantMemory" not in out, (
        "a non-scalar field must still be filtered out"
    )


def test_a_plain_dict_result_still_exports_exactly_as_before():
    """Guard against regressing the existing dict/.reason paths."""
    out = task_runner._exported_variables({"test_code": "x", "n": 1})
    assert out["test_code"] == "x"
    assert out["testCode"] == "x"
    assert out["n"] == 1


def test_a_result_with_neither_reason_nor_model_dump_exports_nothing():
    class _Empty:
        pass

    assert task_runner._exported_variables(_Empty()) == {}
