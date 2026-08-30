"""The ontology rule text-is-plain enforces itself AT THE WRITE (task
b2f29d45).

THE DEFECT THIS PINS. One concept, "text is not plain English", has two
representations that disagree:

  * ``ontology/shapes.ttl`` declares o:text-is-plain, a SPARQL regex over
    the WHOLE rdfs:label / rdfs:comment literal.
  * ``services/ste.py`` normalises the same text at every task and memory
    write, but only OUTSIDE a protected span, and from a hand-kept table
    of 16 contractions plus a semicolon rule that matches ``;`` only when
    a letter follows it.

shapes.ttl's own header names this failure mode: "one concept, two
representations". Measured against the live prism graph on 2026-08-30,
384 stored literals matched the rule and ``ste.apply`` could clear only
88 of them. The residue split two ways:

  * 438 semicolons and 43 contractions sat in PLAIN PROSE, which the
    writer is allowed to fix and did not. The semicolons were almost all
    enumerated clauses ("...; (b) ...", "...; (2) ..."), which
    ``_apply_semicolon``'s ``;\\s*([a-zA-Z])`` pattern cannot see. The
    contractions were forms the regex names but the table omits
    (hasn't, there's, what's, that's, you've, we'll).
  * 105 semicolons and 74 contractions sat INSIDE a protected span - a
    quoted test fixture, quoted owner speech, an inline code span. The
    writer must never touch those, so the rule must not fire on them
    either. A rewrite there would change what the text CLAIMS.

So the rule is unsatisfiable as written, and it grows on every write.

WHAT THESE TESTS REQUIRE.

  1. A write cannot store text that the rule flags. Every alternative the
     shapes.ttl regex names is neutralised in plain prose.
  2. The rule does not fire on a protected span, because the writer is
     forbidden to change one.
  3. Neither of those may cost a hedge. "may have failed" stays "may have
     failed" - a rewrite that drops the hedge is a different claim.

The rule is run HERE the way the graph runs it: the sh:select is read
straight out of shapes.ttl and executed over a small rdflib graph, so
these tests cannot drift from the shipped rule. No test asserts
``stored == normalize(input)``; each names the literal text it expects.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import ste  # noqa: E402

_ONTOLOGY_DIR = _SERVICE_ROOT / "prism_service" / "ontology"
_SHAPES_TTL = _ONTOLOGY_DIR / "shapes.ttl"
_NS = "urn:prism:onto:"
_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")


# ----------------------------------------------------------------------
# The rule, read from the shipped shapes file and run for real.
# ----------------------------------------------------------------------


def _rule_select(rule_name: str) -> str:
    """The sh:select of one SPARQLConstraint in shapes.ttl, with $this
    turned into an ordinary ?this variable so it runs standalone."""
    g = rdflib.Graph()
    g.parse(str(_SHAPES_TTL), format="turtle")
    select = g.value(rdflib.URIRef(_NS + rule_name), _SH.select)
    assert select is not None, f"{rule_name} has no sh:select in shapes.ttl"
    return str(select).replace("$this", "?this")


def _rule_flags(rule_name: str, text: str) -> bool:
    """True when the shipped rule flags ``text`` carried as the
    rdfs:comment of one o:Task - exactly the shape _emit_tasks projects a
    task description into the graph as."""
    g = rdflib.Graph()
    subject = rdflib.URIRef(_NS + "instance/task/pinned")
    g.add((subject, rdflib.RDF.type, rdflib.URIRef(_NS + "Task")))
    g.add((subject, rdflib.RDFS.comment, rdflib.Literal(text)))
    return bool(list(g.query(_rule_select(rule_name))))


def _plain_regex_source() -> str:
    """The regex literal inside text-is-plain's sh:select, unescaped from
    Turtle back to what SPARQL actually compiles."""
    select = _rule_select("text-is-plain")
    m = re.search(r'REGEX\(\?t,\s*"(.*?)",\s*"i"\)', select, re.S)
    assert m, "text-is-plain's sh:select no longer carries a REGEX literal"
    return m.group(1).replace("\\\\", "\\")


# One sample per branch the shipped regex names. Each must TRIP the rule
# today and must come out of the writer clean. Keyed by branch so a
# failure names which half of the rule the writer cannot satisfy; the
# coverage test below proves the set still spans the whole regex.
_BRANCH_SAMPLES = {
    "semicolon": "The seat reads the receipt; the gate waits.",
    "semicolon-before-a-clause-number":
        "The seat reads the receipt; (2) the gate waits.",
    "n't": "The worker hasn't run today.",
    "it's": "It's the only receipt on file.",
    "that's": "That's what the card shows.",
    "what's": "What's on the card is stale.",
    "there's": "There's no receipt on file.",
    "'re": "They're waiting on the seat.",
    "'ll": "We'll record the refusal.",
    "'ve": "You've seen this card before.",
    "I'm": "I'm reading the receipt now.",
    "seamless": "The seat is seamless.",
    "seamlessly": "The seat runs seamlessly.",
    "robust": "The seat is robust.",
    "powerful": "The seat is powerful.",
    "cutting-edge": "The seat is cutting-edge.",
    "effortless": "The seat is effortless.",
    "effortlessly": "The seat runs effortlessly.",
    "blazing-fast": "The seat is blazing-fast.",
}


# ----------------------------------------------------------------------
# Fixtures: prose that trips the rule in every way the regex names.
# ----------------------------------------------------------------------

# Enumerated clauses, the dominant live shape (438 of 543 residual
# semicolons). "; (b)" and "; (2)" have no letter after the semicolon, so
# today's _apply_semicolon leaves both.
_ENUMERATED = (
    "The seat does two things: (a) it reads the receipt; (b) it records "
    "the refusal; (2) it never decides the gate."
)
_ENUMERATED_PLAIN = (
    "The seat does two things: (a) it reads the receipt. (b) it records "
    "the refusal. (2) it never decides the gate."
)

# Contractions the regex names and the 16-entry table omits.
_CONTRACTED = (
    "The worker hasn't run, so there's no receipt and that's what the "
    "card shows. You've seen this and we'll fix it."
)
_CONTRACTED_PLAIN = (
    "The worker has not run, so there is no receipt and that is what the "
    "card shows. You have seen this and we will fix it."
)

# A protected span the writer must never touch: a quoted test fixture and
# an inline code span, both carrying text the rule's regex matches. This
# is a real live shape - a task description quoting the fixture string of
# the test it describes.
_PROTECTED = (
    'The fixture posts body "open a ticket for the PR; it\'s urgent" and '
    "the guard is `if not x: return; # noqa`."
)

# A hedge sentence that trips the rule in the two ways the writer cannot
# handle today - an enumerated semicolon and an untabled contraction - so
# clearing it forces a real rewrite ACROSS the hedge, which is exactly
# where a hedge gets dropped.
_HEDGED = (
    "The receipt may have failed; (2) the seat hasn't run, so there's "
    "possibly no evidence and the gate could still be open."
)
_HEDGED_PLAIN = (
    "The receipt may have failed. (2) the seat has not run, so there is "
    "possibly no evidence and the gate could still be open."
)


# ----------------------------------------------------------------------


def test_a_new_task_write_adds_no_violation():
    """The oracle. Text written through the task write path is stored in a
    form the shipped text-is-plain rule does not flag - in plain prose,
    because the writer fixes it, and in a protected span, because the rule
    must not judge what the writer may not touch.

    Each expected string is named literally. Comparing the stored text
    against normalize() applied to the same input would prove only that
    the function is deterministic.
    """
    stored, _ = ste.normalize(_ENUMERATED, mode="flavored")
    assert stored == _ENUMERATED_PLAIN
    assert not _rule_flags("text-is-plain", stored)

    stored, _ = ste.normalize(_CONTRACTED, mode="flavored")
    assert stored == _CONTRACTED_PLAIN
    assert not _rule_flags("text-is-plain", stored)

    # The writer leaves the protected span byte-identical, on purpose.
    stored, _ = ste.normalize(_PROTECTED, mode="flavored")
    assert stored == _PROTECTED
    # So the rule must not fire on it. Today it does, which is why 58
    # live nodes can never be cleared by any backfill.
    assert not _rule_flags("text-is-plain", stored)


def test_a_rewrite_keeps_every_hedge():
    """A rewrite that clears the violation must not change the claim.
    "may have failed" is not "failed"; "might not have run" is not "did
    not run". Every hedge word survives, and the sentence still says the
    same thing."""
    stored, _ = ste.normalize(_HEDGED, mode="flavored")
    assert stored == _HEDGED_PLAIN
    assert not _rule_flags("text-is-plain", stored)

    for hedge in ("may", "might", "could", "sometimes", "possibly", "likely"):
        sentence = (
            f"The gate {hedge} refuse the receipt; (b) the seat hasn't run."
        )
        expected = (
            f"The gate {hedge} refuse the receipt. (b) the seat has not run."
        )
        out, _ = ste.normalize(sentence, mode="flavored")
        assert out == expected, f"hedge {hedge!r} did not survive"
        assert not _rule_flags("text-is-plain", out)


@pytest.mark.parametrize("branch,sample", sorted(_BRANCH_SAMPLES.items()))
def test_the_rule_and_the_writer_share_one_definition(branch, sample):
    """Drift guard. For every branch the shipped regex names, the writer
    must be able to clear it in plain prose. A branch the normaliser
    cannot fix is a rule no write can ever satisfy - the "one concept,
    two representations" failure shapes.ttl warns about."""
    assert _rule_flags("text-is-plain", sample), (
        f"fixture for {branch!r} does not actually trip the rule")
    out, _ = ste.normalize(sample, mode="flavored")
    assert not _rule_flags("text-is-plain", out), (
        f"ste.normalize cannot clear the rule's own {branch!r} branch: {out!r}")


def test_the_branch_samples_span_the_whole_rule():
    """The drift guard above is only as good as its sample set. Every
    branch of the shipped regex must be exercised by at least one sample,
    so a branch added to shapes.ttl tomorrow cannot slip past unchecked."""
    source = _plain_regex_source()
    # Split on the top-level "|" only: a "|" inside a (...) group belongs
    # to that group's own alternation, which one sample already covers.
    branches, depth, current = [], 0, ""
    for ch in source:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            branches.append(current)
            current = ""
            continue
        current += ch
    branches.append(current)

    unmatched = []
    for branch in branches:
        pattern = re.compile(branch, re.IGNORECASE)
        if not any(pattern.search(s) for s in _BRANCH_SAMPLES.values()):
            unmatched.append(branch)
    assert not unmatched, (
        f"no sample exercises these branches of text-is-plain: {unmatched}")
