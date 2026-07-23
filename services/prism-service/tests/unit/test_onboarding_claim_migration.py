"""Onboard a credential-free instance into the keyed owned system
(task fa52ba9e, decisions mx-935cc2 + mx-30fc0c).

An existing PRISM ran with no accounts. When it auto-updates to the identity
build, the existing owner claims it once (name + email, no setup secret, no db
edit) and is handed their key. The migration is ADDITIVE: it must never reset
or drop anything the owner already had.

These pin the claim + migration at the service layer:
  AC-1  additive migration preserves every existing row (no data loss)
  AC-2  the owner claims with name + email, no secret, and gets a readable key
  AC-4  the claimed owner persists, so the owner is not re-prompted
Re-claim is refused so a second caller can never take over an owned instance.
"""
from pathlib import Path

import pytest

from prism_service.services.workspace_service import WorkspaceService
from prism_service.services.auth_service import AuthService


@pytest.fixture()
def dbpath(tmp_path: Path):
    return tmp_path / "workspace.db"


def _svc(dbpath):
    return WorkspaceService(dbpath)


def test_migration_preserves_existing_rows_no_data_loss(dbpath):
    """AC-1: seed a credential-free db, then re-open (runs migration again).
    Every existing user and token survives, and re-open is idempotent."""
    s = _svc(dbpath)
    s.create_user("owner@x.dev", display_name="Owner", user_id="local-user")
    auth = AuthService(s, mode="local")
    auth.my_access_key("local-user")  # a pre-existing key
    users_before = s._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    toks_before = s._db.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
    s.close()

    s2 = _svc(dbpath)  # simulates the upgraded build opening the same data dir
    try:
        users_after = s2._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        toks_after = s2._db.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
        assert (users_after, toks_after) == (users_before, toks_before), \
            "the migration must be additive: no existing row is dropped"
    finally:
        s2.close()


def test_fresh_instance_is_unclaimed(dbpath):
    s = _svc(dbpath)
    try:
        auth = AuthService(s, mode="local")
        assert auth.is_claimed() is False, "a fresh instance starts unclaimed"
    finally:
        s.close()


def test_owner_claims_with_name_email_no_secret_and_gets_key(dbpath):
    """AC-2: claim takes name + email only. No setup secret, no db edit."""
    s = _svc(dbpath)
    try:
        auth = AuthService(s, mode="local")
        result = auth.claim_instance(name="Siege", email="you@yourdomain.dev")
        assert result.get("key"), "claiming hands the owner their access key"
        assert auth.is_claimed() is True
        owner = auth.claimed_owner()
        assert owner is not None
        assert owner.display_name == "Siege"
        assert owner.email == "you@yourdomain.dev"
    finally:
        s.close()


def test_claimed_key_is_the_readable_access_key(dbpath):
    """The claim key is the same one Settings shows (v7.2.0 my_access_key)."""
    s = _svc(dbpath)
    try:
        auth = AuthService(s, mode="local")
        claimed = auth.claim_instance(name="Siege", email="you@x.dev")
        owner = auth.claimed_owner()
        again = auth.my_access_key(owner.id)
        assert again["secret"] == claimed["key"], \
            "the key handed at claim is the readable Settings key"
    finally:
        s.close()


def test_claimed_owner_persists_across_reopen_no_reprompt(dbpath):
    """AC-4: after claiming, a new service on the same db still sees the owner,
    so the owner is never asked to claim/log in again."""
    s = _svc(dbpath)
    AuthService(s, mode="local").claim_instance(name="Siege", email="you@x.dev")
    s.close()
    s2 = _svc(dbpath)
    try:
        auth2 = AuthService(s2, mode="local")
        assert auth2.is_claimed() is True
        assert auth2.claimed_owner().display_name == "Siege"
    finally:
        s2.close()


def test_reclaim_is_refused_no_takeover(dbpath):
    """A claimed instance cannot be re-claimed by a second caller."""
    s = _svc(dbpath)
    try:
        auth = AuthService(s, mode="local")
        auth.claim_instance(name="Siege", email="you@x.dev")
        with pytest.raises(Exception):
            auth.claim_instance(name="Attacker", email="bad@x.dev")
        assert auth.claimed_owner().display_name == "Siege"
    finally:
        s.close()
