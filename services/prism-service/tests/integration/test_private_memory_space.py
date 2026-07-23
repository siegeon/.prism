"""Every user gets a private memory space (task e8059640, decisions
mx-935cc2 + mx-43606b).

Memory is the SAME structure of knowledge whether shared or private; privacy is
a scope on the entry: owner_user_id empty = SHARED project memory, set = PRIVATE
to that user (in the real product it is inherited from the source connection).

These pin the data model + service scope, the expensive-to-change part:
  AC-1  the owner scope is additive: an old entry with no owner field is shared
  AC-4  store defaults to shared; a private store stamps the owner
  AC-2  recall returns shared + the caller's own private, never a peer's private
"""
import json
from pathlib import Path

import pytest

from prism_service.services.memory_service import MemoryService


@pytest.fixture()
def mem(tmp_path: Path):
    return MemoryService(str(tmp_path))


def _store(mem, name, desc, owner=""):
    return mem.store(
        domain="architecture", name=name, description=desc,
        type="decision", classification="foundational", owner_user_id=owner,
    )


def test_store_defaults_to_shared(mem):
    """AC-4: no owner given -> shared (owner_user_id empty)."""
    e = _store(mem, "shared-thing", "a shared project decision about gates")
    assert e.owner_user_id == ""


def test_store_private_stamps_owner(mem):
    """AC-4: a private store records the owner."""
    e = _store(mem, "my-note", "a personal note from my email", owner="user-A")
    assert e.owner_user_id == "user-A"


def test_recall_gives_shared_plus_own_private_not_peers(mem):
    """AC-2: A recalls shared + A's private; never B's private."""
    _store(mem, "shared-gate", "shared decision gate honesty receipt", owner="")
    _store(mem, "a-private", "gate reminder about Acme vendor quote due Friday", owner="user-A")
    _store(mem, "b-private", "gate note on Dana's contract renewal with legal", owner="user-B")

    got = mem.recall(query="gate", domain="architecture", limit=10,
                     caller_user_id="user-A")
    owners = {e.owner_user_id for e in got}
    names = {e.name for e in got}
    assert "b-private" not in names, "A must never receive B's private memory"
    assert "user-B" not in owners
    # A does get the shared one and its own private one
    assert "shared-gate" in names
    assert "a-private" in names


def test_old_entry_without_owner_field_is_shared(mem, tmp_path):
    """AC-1: additive. A pre-existing JSONL line with no owner_user_id loads as
    shared, so the migration never strands old memory."""
    # write a legacy entry by hand, without the owner_user_id field, into the
    # real mulch layout (MemoryService stores under <dir>/expertise/).
    f = tmp_path / "expertise" / "architecture.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "id": "mx-legacy", "type": "decision", "name": "legacy",
        "description": "an old memory from before scopes existed",
        "classification": "foundational", "status": "active",
        "domain": "architecture", "valid_at": "2026-01-01T00:00:00+00:00",
    }) + "\n", encoding="utf-8")
    got = mem.recall(query="old memory", domain="architecture", limit=10,
                     caller_user_id="user-A")
    legacy = [e for e in got if e.id == "mx-legacy"]
    assert legacy and legacy[0].owner_user_id == "", \
        "a legacy entry with no owner field must read as shared"
