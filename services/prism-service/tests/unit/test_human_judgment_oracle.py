"""oracle_spec.is_human_judgment — the objective-vs-subjective split (task
eaafdf75, owner 2026-07-19).

A MACHINE seat may sign off only an OBJECTIVE-OBSERVABLE oracle (a test suite
passes, an http probe returns ok). Anything validated VISUALLY — a browser /
render check, or a manual tooth — is a HUMAN judgment and must stay with the
person. This classifier is the single primitive both the adjudicator and the
release workability self-check consult.
"""
from prism_service.services import oracle_spec as osp
from prism_service.services.oracle_spec import Assertion, OracleSpec


def _spec(adapter, positive=()):
    return OracleSpec(adapter=adapter, target="t", positive=tuple(positive))


def test_browser_adapter_is_human_judgment():
    assert osp.is_human_judgment(_spec(osp.ADAPTER_BROWSER)) is True


def test_manual_tooth_is_human_judgment():
    spec = _spec(osp.ADAPTER_PYTEST,
                 [Assertion("m", "positive", "manual", True)])
    assert osp.is_human_judgment(spec) is True


def test_pytest_is_objective():
    spec = _spec(osp.ADAPTER_PYTEST,
                 [Assertion("t", "positive", "pytest_pass", True)])
    assert osp.is_human_judgment(spec) is False


def test_http_probe_is_objective():
    spec = _spec(osp.ADAPTER_HTTP,
                 [Assertion("h", "positive", "status_ok", True)])
    assert osp.is_human_judgment(spec) is False


def test_demo_task_derives_a_human_judgment_oracle():
    """A demo ticket's DERIVED oracle must classify as human-judgment (this is
    the class the machine used to auto-pass)."""
    from types import SimpleNamespace
    task = SimpleNamespace(oracle="the customer can read the page",
                           proof_type="demo", verify=[], id="x")
    spec = osp.OracleSpec.from_task(task)
    assert osp.is_human_judgment(spec) is True
