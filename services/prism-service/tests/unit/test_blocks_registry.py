"""Pin test: the multiplier-block registry (prism_service/blocks/) --
owner 2026-09-13, task b490fabc: "you have not got the hang of creating
the multiplier blocks that are pydantic".

Importing the three seat modules that now declare a block must register
each one under a stable id, typed as a real `Block` pydantic instance --
never a bare Python branch invisible to anything outside its own module.
A source/registry test rather than a live-daemon test: registration
happens at import time, so this needs no running project/task fixtures.
"""
import pytest

from prism_service.blocks import Block, get_block, list_blocks, run_block

# Importing these modules is what triggers register_block(...) at their
# module level -- see design_packet.py / resume_actuator.py /
# gate_adjudicator.py, each just after the function it wraps.
from prism_service.services import design_packet  # noqa: F401
from prism_service.services import gate_adjudicator  # noqa: F401
from prism_service.services import resume_actuator  # noqa: F401

EXPECTED_BLOCK_IDS = {
    "certainty.derive_oracle": "design_packet",
    "resume.clear_stale_park": "resume_actuator",
    "adjudicator.drain": "gate_adjudicator",
}


@pytest.mark.parametrize("block_id,owner_seat", EXPECTED_BLOCK_IDS.items())
def test_each_landing_one_block_is_registered(block_id, owner_seat):
    block = get_block(block_id)
    assert isinstance(block, Block)
    assert block.id == block_id
    assert block.owner_seat == owner_seat
    # A real unit of behaviour, never a fabricated placeholder: every
    # field the canvas would show a human must carry real content.
    assert block.title.strip()
    assert block.description.strip()
    assert block.kind in ("deterministic", "agentic", "http")
    assert block.on_failure in ("continue", "stop")


def test_registered_ids_cover_at_least_the_landing_one_set():
    ids = {b.id for b in list_blocks()}
    assert EXPECTED_BLOCK_IDS.keys() <= ids


def test_run_block_rejects_an_unknown_id():
    with pytest.raises(KeyError):
        run_block("no.such.block", project="prism")


def test_register_block_rejects_a_different_callable_reusing_an_id():
    from prism_service.blocks import register_block

    def _impostor():
        return None

    with pytest.raises(ValueError):
        register_block(
            Block(id="certainty.derive_oracle", title="impostor",
                  kind="deterministic", owner_seat="nobody"),
            _impostor)


def test_re_registering_the_same_callable_is_a_no_op():
    """A module re-import under pytest calls register_block again with
    the SAME function object -- this must never raise, or every second
    test module that imports design_packet would fail collection."""
    from prism_service.blocks import register_block
    from prism_service.services.design_packet import (
        CERTAINTY_DERIVE_ORACLE_BLOCK, _run_derive_oracle_checkability)

    register_block(CERTAINTY_DERIVE_ORACLE_BLOCK, _run_derive_oracle_checkability)
    assert get_block("certainty.derive_oracle") is CERTAINTY_DERIVE_ORACLE_BLOCK


def test_block_wrappers_resolve_by_module_global_name_not_a_captured_ref():
    """Each wrapper (_run_sweep_once, _run_derive_oracle_checkability,
    _run_clear_stale_park_text) must call its target by NAME so a test's
    monkeypatch.setattr on the seat module still takes effect -- a
    registry that captured the function OBJECT at import time would
    silently keep calling the pre-patch original (the exact regression
    test_gate_adjudicator_deploy_forces_resweep.py caught live during
    this landing)."""
    from prism_service.services import gate_adjudicator as ga

    calls = []
    original = ga.sweep_once
    try:
        ga.sweep_once = lambda *a, **kw: calls.append((a, kw)) or ["patched"]
        result = run_block("adjudicator.drain", project="*",
                           kwargs={"force": True, "force_backoff": False})
        assert result == ["patched"]
        assert calls == [((), {"force": True, "force_backoff": False})]
    finally:
        ga.sweep_once = original
