"""Slice C — a first-class PRISM User entity + a request principal — TDD red.

PRISM has NO user/principal today (scoping is project-only via ?project=).
This slice adds:

  Seam 1 (model + persistence) — UserService(temp db) cloning the
    TaskService store shape (DATA_DIR-level, NOT per-project): create a
    User {id like 'usr_xxxx', name, handle, jira_account_id=''}, get/list
    it back from a SEPARATE service over the same users.db (on-disk, not
    in-memory).
  Seam 2 (Jira link) — link_jira(user_id, account_id) sets jira_account_id
    (the account id/handle only — NEVER a token) and persists it.
  Seam 3 (model) — the User dataclass is dependency-free, carries the
    fields, and survives serialization.
  Seam 4 (request principal) — PrismRequestContext carries a `principal`
    field (a User id, default '') that can be set.

ALL FAIL today: models/user.py + services/users.py don't exist and
PrismRequestContext has no `principal` field.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _svc(tmp_path, name="users.db"):
    from prism_service.services.users import UserService
    return UserService(str(tmp_path / name))


# ----------------------------------------------------------------------
# Seam 1 — model field + on-disk persistence round-trip
# ----------------------------------------------------------------------

def test_create_user_has_usr_id_and_defaults(tmp_path):
    u = _svc(tmp_path).create(name="Ada Lovelace", handle="ada")
    assert re.fullmatch(r"usr_[0-9a-f]{4,}", u.id), u.id
    assert u.name == "Ada Lovelace"
    assert u.handle == "ada"
    assert u.jira_account_id == ""
    assert u.created_at


def test_create_persists_user_across_instances(tmp_path):
    """create() persists; a SEPARATE UserService over the same db hydrates
    it back via get() — proves on-disk, not in-memory."""
    svc = _svc(tmp_path)
    u = svc.create(name="Ada", handle="ada")

    fresh = _svc(tmp_path)  # new connection, same file
    got = fresh.get(u.id)
    assert got is not None
    assert got.name == "Ada" and got.handle == "ada"
    assert u.id in {x.id for x in fresh.list()}


def test_get_missing_user_returns_none(tmp_path):
    assert _svc(tmp_path).get("usr_nope") is None


# ----------------------------------------------------------------------
# Seam 2 — Jira link stores the account id (NEVER a token), persisted
# ----------------------------------------------------------------------

def test_link_jira_sets_and_persists_account_id(tmp_path):
    svc = _svc(tmp_path)
    u = svc.create(name="Ada", handle="ada")
    linked = svc.link_jira(u.id, "557058:abc-123")
    assert linked.jira_account_id == "557058:abc-123"

    fresh = _svc(tmp_path)
    assert fresh.get(u.id).jira_account_id == "557058:abc-123"


def test_link_jira_unknown_user_returns_none(tmp_path):
    assert _svc(tmp_path).link_jira("usr_nope", "acct") is None


# ----------------------------------------------------------------------
# Seam 3 — dataclass is dependency-free, serializes
# ----------------------------------------------------------------------

def test_user_dataclass_serializes(tmp_path):
    u = _svc(tmp_path).create(name="Ada", handle="ada")
    d = dataclasses.asdict(u)
    assert d["jira_account_id"] == ""
    assert set(d) >= {"id", "name", "handle", "jira_account_id", "created_at"}


# ----------------------------------------------------------------------
# Seam 4 — PrismRequestContext carries a settable `principal` (default '')
# ----------------------------------------------------------------------

def test_request_context_principal_defaults_empty():
    from prism_service.mcp.request_context import PrismRequestContext
    assert PrismRequestContext().principal == ""


def test_request_context_principal_can_be_set():
    from prism_service.mcp.request_context import PrismRequestContext
    ctx = PrismRequestContext(principal="usr_abcd")
    assert ctx.principal == "usr_abcd"
