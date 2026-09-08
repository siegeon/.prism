"""Task dc815149: does a proposed cut RUN IN PARALLEL?

A slice exists to run N agents at the same time and then to put the results
back together. Two slices that claim the same path cannot run at the same
time, so a cut that overlaps has failed at its only job.

Pure Python over the data the caller passes in. No file access, no process
launch, no model client -- a codified orchestration node that calls a model
puts the cost back into the very phase this exists to make free (AC-6).

The shipped helpers `overlapping_allowed_files` and `can_run_parallel`
(services/conductor_service.py:756-772) compare paths with a plain set
intersection, so `api/` and `api/brain.py` read as disjoint. That file is a
control_plane.POLICY_FILES entry, so this module replaces neither: it
computes containment itself and leaves those two where they are.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations


@dataclass(frozen=True)
class Slice:
    """One proposed child of the cut."""

    id: str
    title: str = ""
    allowed_files: tuple = ()


@dataclass(frozen=True)
class Collision:
    """One shared path, and the pair of slices that path forces together."""

    path: str
    slice_a: str
    slice_b: str

    def to_dict(self) -> dict:
        return {"path": self.path, "slice_a": self.slice_a,
                "slice_b": self.slice_b}


@dataclass(frozen=True)
class PartitionResult:
    parallel_safe: bool
    fan_out: int
    collisions: list = field(default_factory=list)
    slice_ids: list = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {"parallel_safe": self.parallel_safe, "fan_out": self.fan_out,
                "collisions": [c.to_dict() for c in self.collisions],
                "slice_ids": list(self.slice_ids), "reason": self.reason}


@dataclass(frozen=True)
class FanInResult:
    complete: bool
    assembler: str = ""
    parent_must_demonstrate: list = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {"complete": self.complete, "assembler": self.assembler,
                "parent_must_demonstrate": list(self.parent_must_demonstrate),
                "reason": self.reason}


def normalise(path: str) -> str:
    """POSIX separators, no `./` prefix, no repeats, no trailing separator."""
    text = str(path or "").replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def contains(a: str, b: str) -> bool:
    """True when `a` IS `b`, or when `b` sits below the directory `a`."""
    left, right = normalise(a), normalise(b)
    if not left or not right:
        return False
    return left == right or right.startswith(left + "/")


def partition(slices) -> PartitionResult:
    """Report whether every slice can start at the same time.

    An overlap is REFUSED and named, never repaired with an ordering edge:
    the caller re-cuts the slices. Serialising is what an operator already
    did by hand, and is the outcome this node exists to prevent.
    """
    rows = list(slices or [])
    ids = [s.id for s in rows]
    found: set[tuple[str, str, str]] = set()

    for left, right in combinations(rows, 2):
        pair = tuple(sorted((left.id, right.id)))
        for one in left.allowed_files:
            for other in right.allowed_files:
                if contains(one, other):
                    found.add((normalise(other), pair[0], pair[1]))
                elif contains(other, one):
                    found.add((normalise(one), pair[0], pair[1]))

    collisions = [Collision(*row) for row in sorted(found)]
    if collisions:
        first = collisions[0]
        return PartitionResult(
            parallel_safe=False, fan_out=0, collisions=collisions,
            slice_ids=ids,
            reason=(f"{len(collisions)} shared path(s) force this cut to "
                    f"serialise; {first.path} joins {first.slice_a} and "
                    f"{first.slice_b}. Re-cut the slices so the allowlists "
                    f"are disjoint."))
    return PartitionResult(
        parallel_safe=True, fan_out=len(rows), collisions=[], slice_ids=ids,
        reason=f"parallel-safe: {len(rows)} slice(s) can start at the same time")


def fan_in(slices, assembler_id: str, parent_clauses: str = "") -> FanInResult:
    """Name the assembler slice and what the PARENT must demonstrate itself.

    A cut proved parallel-safe with nobody assembling leaves N green slices
    and an undone parent, so a cut with no assembler is INCOMPLETE.
    """
    ids = [s.id for s in list(slices or [])]
    clauses = [line.strip() for line in str(parent_clauses or "").splitlines()
               if line.strip()]
    wanted = str(assembler_id or "")
    if not wanted:
        return FanInResult(
            complete=False, assembler="", parent_must_demonstrate=clauses,
            reason="no assembler slice: name the slice that puts the results "
                   "back together, or the parent stays undone")
    if wanted not in ids:
        return FanInResult(
            complete=False, assembler="", parent_must_demonstrate=clauses,
            reason=f"assembler {wanted} is not one of the slices {ids}")
    return FanInResult(
        complete=True, assembler=wanted, parent_must_demonstrate=clauses,
        reason=f"{wanted} assembles the cut; the parent demonstrates "
               f"{len(clauses)} clause(s) itself")
