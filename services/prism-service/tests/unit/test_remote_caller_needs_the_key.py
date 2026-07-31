"""A caller from another machine must present the owner's access key.

Owner, 2026-07-30, locked out of their own work from a second machine:
"i tried to log into the endpoint from another machine - and i have no way to
do so?" ... "i also have the key on the second machine that should be enough
to access it".

Two facts collided. The daemon binds 0.0.0.0 (main.py), so it IS reachable
across the network. But local mode resolved EVERY caller to the owner
principal without looking at any credential, so:
  - a stranger on the same network got the owner's data, and
  - the access key the owner already had was never consulted, so there was
    nothing to "log in" WITH.

The fix keeps loopback exactly as permissive as it was (no lockout risk on the
machine running PRISM) and requires the owner's access key from anywhere else.
The key is the one already surfaced at /api/auth/my-key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_service.services.auth_service import AuthenticationRequired, AuthService
from prism_service.services.workspace_service import WorkspaceService


@pytest.fixture()
def local_auth(tmp_path: Path):
    svc = WorkspaceService(tmp_path / "workspace.db")
    try:
        yield AuthService(svc, mode="local")
    finally:
        # Close the thread-local sqlite handle so Windows can release the file.
        svc.close()


# --- loopback stays trusted: the owner's own machine is unchanged -----------

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "127.0.0.5"])
def test_loopback_needs_no_credential(local_auth, host):
    who = local_auth.resolve_principal(None, client_host=host)
    assert who.user_id == AuthService.LOCAL_USER_ID
    assert who.role == "owner"


def test_unknown_peer_is_treated_as_trusted_for_direct_calls(local_auth):
    # client_host=None means "no peer information" (a direct in-process call).
    # It must stay permissive or every existing local-mode caller breaks.
    assert local_auth.resolve_principal(None).user_id == AuthService.LOCAL_USER_ID


# --- a remote caller must prove the key ------------------------------------

def test_remote_caller_without_a_key_is_refused(local_auth):
    with pytest.raises(AuthenticationRequired):
        local_auth.resolve_principal(None, client_host="10.1.10.42")


def test_remote_caller_with_a_wrong_key_is_refused(local_auth):
    with pytest.raises(AuthenticationRequired):
        local_auth.resolve_principal("Bearer not-the-real-key",
                                     client_host="10.1.10.42")


def test_remote_caller_with_the_owner_key_gets_in(local_auth):
    # Exactly what the owner has on the second machine.
    key = local_auth.my_access_key(AuthService.LOCAL_USER_ID)["secret"]
    who = local_auth.resolve_principal(f"Bearer {key}", client_host="10.1.10.42")
    assert who.user_id == AuthService.LOCAL_USER_ID
    assert who.role == "owner"


def test_the_refusal_says_how_to_get_in(local_auth):
    # A 401 that does not name the remedy is why this cost the owner an hour.
    with pytest.raises(AuthenticationRequired) as caught:
        local_auth.resolve_principal(None, client_host="10.1.10.42")
    msg = str(caught.value).lower()
    assert "key" in msg, "the refusal must name the access key as the credential"


def test_a_rotated_key_locks_the_old_one_out(local_auth):
    stale = local_auth.my_access_key(AuthService.LOCAL_USER_ID)["secret"]
    fresh = local_auth.rotate_access_key(AuthService.LOCAL_USER_ID)["secret"]
    assert fresh != stale
    who = local_auth.resolve_principal(f"Bearer {fresh}", client_host="10.1.10.42")
    assert who.user_id == AuthService.LOCAL_USER_ID
    with pytest.raises(AuthenticationRequired):
        local_auth.resolve_principal(f"Bearer {stale}", client_host="10.1.10.42")
