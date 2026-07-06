"""Canon tests for the single source-of-truth role/tier registry.

Pins prism_service.models.roles: the 3 canonical roles (sm/qa/dev), the
alias-folding of every legacy persona label, the step->role table for
every WORKFLOW_STEPS id (gates adjudicated by the Steward), and the
serializable registry() shape mirrored to /api/roles + the MCP read.

This suite should PASS immediately — roles.py already exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ----------------------------------------------------------------------
# The 3 canonical roles — ids kept stable for schema/task_history compat.
# ----------------------------------------------------------------------


def test_roles_are_exactly_sm_qa_dev():
    from prism_service.models import roles
    assert set(roles.ROLES) == {"sm", "qa", "dev"}


def test_role_labels_are_model_agnostic_hats():
    from prism_service.models import roles
    labels = {rid: r.label for rid, r in roles.ROLES.items()}
    assert labels == {"sm": "Steward", "qa": "Verifier", "dev": "Builder"}


def test_role_tiers_match_wargame_economics():
    from prism_service.models import roles
    tiers = {rid: r.tier for rid, r in roles.ROLES.items()}
    assert tiers == {"sm": "frontier", "qa": "balanced", "dev": "fast"}


# ----------------------------------------------------------------------
# normalize_role folds every legacy persona/alias into the canon.
# ----------------------------------------------------------------------


def test_normalize_role_folds_known_aliases():
    from prism_service.models import roles
    assert roles.normalize_role("architect") == "sm"
    assert roles.normalize_role("lead") == "sm"
    assert roles.normalize_role("general") == "dev"
    assert roles.normalize_role("tester") == "qa"


def test_normalize_role_none_defaults_to_dev():
    from prism_service.models import roles
    assert roles.normalize_role(None) == "dev"
    assert roles.normalize_role("") == "dev"


def test_normalize_role_is_case_and_whitespace_insensitive():
    from prism_service.models import roles
    assert roles.normalize_role("  Architect ") == "sm"
    assert roles.normalize_role("TESTER") == "qa"


def test_normalize_role_unknown_label_falls_to_default():
    from prism_service.models import roles
    assert roles.normalize_role("wat-is-this") == roles.DEFAULT_ROLE == "dev"


def test_canonical_ids_normalize_to_themselves():
    from prism_service.models import roles
    for rid in roles.ROLES:
        assert roles.normalize_role(rid) == rid


# ----------------------------------------------------------------------
# role_for_step covers EVERY WORKFLOW_STEPS id; gates -> Steward (sm).
# ----------------------------------------------------------------------


def test_role_for_step_covers_every_workflow_step():
    from prism_service.models import roles
    from prism_service.models.workflow import WORKFLOW_STEPS
    for step in WORKFLOW_STEPS:
        rid = roles.role_for_step(step["id"])
        assert rid in roles.ROLES, (
            f"step {step['id']!r} maps to unknown role {rid!r}"
        )


def test_role_for_step_gates_are_stewarded():
    from prism_service.models import roles
    from prism_service.models.workflow import WORKFLOW_STEPS
    gate_ids = [s["id"] for s in WORKFLOW_STEPS if s["type"] == "gate"]
    assert gate_ids, "expected at least one gate in WORKFLOW_STEPS"
    for gid in gate_ids:
        assert roles.role_for_step(gid) == "sm", (
            f"gate {gid!r} must be adjudicated by the Steward (sm)"
        )


def test_role_for_step_agent_steps_match_workflow_agent():
    """Non-gate agent steps normalize to the workflow's declared agent."""
    from prism_service.models import roles
    from prism_service.models.workflow import WORKFLOW_STEPS
    for s in WORKFLOW_STEPS:
        if s["type"] == "gate":
            continue
        assert roles.role_for_step(s["id"]) == roles.normalize_role(s["agent"])


def test_role_for_step_unknown_and_none_default_to_dev():
    from prism_service.models import roles
    assert roles.role_for_step(None) == "dev"
    assert roles.role_for_step("no_such_step") == "dev"


# ----------------------------------------------------------------------
# registry() — the serializable shape /api/roles + the MCP read return.
# ----------------------------------------------------------------------


def test_registry_top_level_shape():
    from prism_service.models import roles
    reg = roles.registry()
    assert set(reg) == {"tiers", "efforts", "roles", "step_roles"}


def test_registry_tiers_include_frontier_balanced_fast():
    from prism_service.models import roles
    tiers = roles.registry()["tiers"]
    for name in ("frontier", "balanced", "fast"):
        assert name in tiers, f"tier {name!r} missing from registry"
        assert isinstance(tiers[name], str) and tiers[name].strip()


def test_registry_roles_are_the_canon_with_full_fields():
    from prism_service.models import roles
    reg = roles.registry()
    reg_roles = reg["roles"]
    assert set(reg_roles) == {"sm", "qa", "dev"}
    for rid, r in reg_roles.items():
        for field in ("id", "label", "tier", "effort", "purpose", "tier_desc"):
            assert field in r, f"role {rid!r} registry dict missing {field!r}"
        assert r["id"] == rid
        assert r["tier"] in reg["tiers"]
        assert r["tier_desc"] == roles.TIERS[r["tier"]]


def test_registry_efforts_and_step_roles():
    from prism_service.models import roles
    reg = roles.registry()
    assert list(reg["efforts"]) == list(roles.EFFORTS)
    # step_roles mirrors the STEP_ROLES table and every value is a canon id.
    assert reg["step_roles"] == dict(roles.STEP_ROLES)
    for step_id, rid in reg["step_roles"].items():
        assert rid in roles.ROLES
