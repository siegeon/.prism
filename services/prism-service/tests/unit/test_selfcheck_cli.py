"""`prism selfcheck` wiring (task e5fbec61) — the workflow workability gate.

Guards against silent drift: every file in the workability set must exist (a
renamed/removed test would otherwise quietly shrink what "workable" means), and
the CLI must expose the `selfcheck` subcommand.
"""
from pathlib import Path

import prism_service
from prism_service.cli import prism_cli


def test_workability_tests_all_exist():
    svc_root = Path(prism_service.__file__).resolve().parent.parent
    tests_dir = svc_root / "tests"
    missing = [t for t in prism_cli.WORKABILITY_TESTS
               if not (tests_dir / t).exists()]
    assert not missing, f"workability test set drifted; missing: {missing}"


def test_selfcheck_subcommand_registered():
    parser = prism_cli._build_parser()
    ns = parser.parse_args(["selfcheck"])
    assert ns.func is prism_cli.cmd_selfcheck


def test_workability_set_covers_the_human_sign_off():
    # The whole point: the human-judgment gate test must be in the gate set.
    joined = " ".join(prism_cli.WORKABILITY_TESTS)
    assert "test_gate_adjudicator_seat.py" in joined
    assert "test_human_judgment_oracle.py" in joined
