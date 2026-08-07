"""RED - the distinct-actor rule compares raw TEXT, not identity (task
a4e41c35).

``same_actor_override_reason`` (conductor_service.py:523-541) lowercases the
overriding actor string and the work-producing actor strings and compares
them as text. Two DIFFERENT raw strings that are the SAME real person/agent
(e.g. a session id vs. that same actor's email, or two differently-cased
aliases logged by two callers) slip past the guard: the override is a
same-actor bypass that reads as "distinct" because the strings differ.

The team-work-hub epic (task 0784729f, commits 411df03/efbd08f on branch
prism/ws/0784729f-5e34-4195-87db-5b54f8ad91cc) introduces the real join:
``prism_service.services.actor_service.ActorService.resolve()`` ->
``prism_service.models.actor.Actor`` (kinds human/agent/machine/unknown).
That branch has NOT merged into this checkout (see
test_actor_resolver_dependency_is_undeclared_here below, which pins the
absence honestly) so this suite injects a FAKE resolver via the seam
``conductor_service._resolve_actor_identity`` to prove the JOIN LOGIC itself
-- compare by ``Actor.id``, never by the raw string -- independent of
whether the real resolver module exists yet in this tree.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import conductor_service as cs  # noqa: E402


@dataclass(frozen=True)
class _FakeActor:
    """Stand-in for prism_service.models.actor.Actor - same shape (an
    ``id`` that is the real join key, plus a display_name for messages)."""

    id: str
    display_name: str = ""


def _fake_resolver(mapping: dict):
    """Return a stub matching the ``_resolve_actor_identity(raw) -> Actor``
    seam: real strings resolve per `mapping`, anything else raises KeyError
    exactly like a broken lookup would."""

    def _resolve(raw: str) -> _FakeActor:
        return mapping[raw]

    return _resolve


# ---------------------------------------------------------------------
# AC-1: same identity, different raw strings -> REFUSED.
# ---------------------------------------------------------------------


def test_same_resolved_identity_refuses_even_with_different_raw_strings(
    monkeypatch,
):
    monkeypatch.setattr(
        cs,
        "_resolve_actor_identity",
        _fake_resolver(
            {
                "alice@example.com": _FakeActor("user:1", "Alice"),
                "session-alice-42": _FakeActor("user:1", "Alice"),
            }
        ),
    )
    reason = cs.same_actor_override_reason(
        "alice@example.com", ["session-alice-42"]
    )
    assert reason, (
        "override_actor and the work producer resolve to the SAME Actor "
        "(user:1) even though the raw strings differ - must be refused"
    )
    low = reason.lower()
    assert "actor" in low and ("same" in low or "distinct" in low), reason


# ---------------------------------------------------------------------
# AC-2: genuinely distinct identity -> ALLOWED.
# ---------------------------------------------------------------------


def test_distinct_resolved_identity_is_allowed(monkeypatch):
    monkeypatch.setattr(
        cs,
        "_resolve_actor_identity",
        _fake_resolver(
            {
                "bob@example.com": _FakeActor("user:2", "Bob"),
                "session-alice-42": _FakeActor("user:1", "Alice"),
            }
        ),
    )
    reason = cs.same_actor_override_reason(
        "bob@example.com", ["session-alice-42"]
    )
    assert reason == "", (
        f"bob (user:2) is a distinct actor from alice (user:1) and must be "
        f"allowed to override, got: {reason!r}"
    )


# ---------------------------------------------------------------------
# AC-3: unresolvable strings must not collide with EACH OTHER (today's
# legacy behavior for "unknown" actors, preserved through the join).
# ---------------------------------------------------------------------


def test_unresolvable_strings_do_not_collide_with_each_other(monkeypatch):
    monkeypatch.setattr(
        cs,
        "_resolve_actor_identity",
        _fake_resolver(
            {
                "garbage-a": _FakeActor("unknown:garbage-a"),
                "garbage-b": _FakeActor("unknown:garbage-b"),
            }
        ),
    )
    reason = cs.same_actor_override_reason("garbage-a", ["garbage-b"])
    assert reason == "", (
        "two different unresolvable strings must not be treated as the "
        f"same actor just because both are 'unknown', got: {reason!r}"
    )


def test_identical_unresolvable_strings_still_refuse(monkeypatch):
    monkeypatch.setattr(
        cs,
        "_resolve_actor_identity",
        _fake_resolver({"garbage-a": _FakeActor("unknown:garbage-a")}),
    )
    reason = cs.same_actor_override_reason("garbage-a", ["garbage-a"])
    assert reason, "the identical unresolvable string is still the same actor"


# ---------------------------------------------------------------------
# AC-4: machine seats keep working through the join (resolve by name,
# deterministic id -> same seat refuses, different seat allows).
# ---------------------------------------------------------------------


def test_machine_seats_keep_working_through_the_join(monkeypatch):
    monkeypatch.setattr(
        cs,
        "_resolve_actor_identity",
        _fake_resolver(
            {
                "conductor-adjudicator": _FakeActor(
                    "machine:conductor-adjudicator"
                ),
                "conductor-autoclear": _FakeActor("machine:conductor-autoclear"),
            }
        ),
    )
    same = cs.same_actor_override_reason(
        "conductor-adjudicator", ["conductor-adjudicator"]
    )
    assert same, "the same machine seat overriding its own work is refused"

    distinct = cs.same_actor_override_reason(
        "conductor-adjudicator", ["conductor-autoclear"]
    )
    assert distinct == "", (
        f"two distinct machine seats must be allowed, got: {distinct!r}"
    )


# ---------------------------------------------------------------------
# AC-5 (likely_misfire guard): a FAILED identity lookup must never be the
# reason a same-actor override is silently ACCEPTED. Once the resolver is
# wired (identity of the overriding actor resolves), a lookup failure for
# one producer row fails CLOSED (refuses) rather than falling back to a
# raw-string compare that could read the differing strings as "distinct".
# ---------------------------------------------------------------------


def test_a_failed_lookup_never_silently_accepts_a_same_actor_override(
    monkeypatch,
):
    def _resolve(raw: str) -> _FakeActor:
        if raw == "alice@example.com":
            return _FakeActor("user:1", "Alice")
        raise RuntimeError("lookup backend unreachable")

    monkeypatch.setattr(cs, "_resolve_actor_identity", _resolve)

    reason = cs.same_actor_override_reason(
        "alice@example.com", ["session-that-fails-to-resolve"]
    )
    assert reason, (
        "a producer row whose identity lookup raised must NOT be silently "
        f"treated as distinct — expected a refusal, got: {reason!r}"
    )
    low = reason.lower()
    assert "lookup" in low or "fail" in low or "verif" in low, reason


# ---------------------------------------------------------------------
# Pins the actual state of THIS checkout: the epic branch that introduces
# services/actor_service.py has not merged here, so the real import fails
# and same_actor_override_reason must fall back to its pre-join string
# compare WHOLESALE (never per-call) rather than crash. This is the honest
# "depends on prism/ws/0784729f-5e34-4195-87db-5b54f8ad91cc landing" signal
# -- if this test ever goes red because the import starts succeeding, that
# is good news (the epic branch merged) and this test should be deleted.
# ---------------------------------------------------------------------


def test_actor_resolver_dependency_is_undeclared_here():
    with pytest.raises(ImportError):
        import prism_service.services.actor_service  # noqa: F401

    # With the resolver absent, the pre-join string-compare path must still
    # do its job for the exact same/distinct cases it always has.
    assert cs.same_actor_override_reason("S1", ["S1"]) != ""
    assert cs.same_actor_override_reason("S1", ["S2"]) == ""
    assert cs.same_actor_override_reason("S1", ["s1"]) != "", (
        "legacy case-insensitive compare must be unchanged while the "
        "resolver is not wired"
    )
