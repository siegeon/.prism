"""The ontology's own text is Simplified Technical English (task
5ac5d04c "The ontology has a rule that content reads as plain English").

The ontology declares a SHACL rule (text-is-plain, shapes.ttl) that
checks a Task/Decision/Term/Agent's text for the regex-checkable subset
of STE. This test turns that rule on the ontology's OWN source files:
every rdfs:label, rdfs:comment, sh:name, sh:description, and sh:message
literal in model.ttl, model-*.ttl, shapes.ttl, and shapes-*.ttl must
itself read as plain English, by the SAME normalizer that checks task
and memory text (services/ste.py) — no contraction, no semicolon, no
marketing word, no filler/nominalisation/phrasal-verb fix, no long
sentence, no passive voice, no present-perfect. "flavored" mode, since
these are prose comments/descriptions, not strict machine instructions.

vocab.json carries no free-text description strings today (task
5ac5d04c checked: every value is a short enum word), but this test
still walks every string value so a future description-like entry is
caught the moment it lands, per vocab.py's own header ("regenerate,
never hand-edit").
"""

from __future__ import annotations

import json
from pathlib import Path

import rdflib

from prism_service.services import ste

_HERE = Path(__file__).resolve()
_ONTOLOGY_DIR = _HERE.parent.parent.parent / "prism_service" / "ontology"

_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_SH = "http://www.w3.org/ns/shacl#"

# The literal-carrying predicates this test holds to STE — every one a
# human reads as prose, never an id, an IRI-local-name, or a path.
_TEXT_PREDICATES = {
    f"{_RDFS}label", f"{_RDFS}comment",
    f"{_SH}name", f"{_SH}description", f"{_SH}message",
}

_TTL_FILES = sorted(_ONTOLOGY_DIR.glob("model*.ttl")) + sorted(
    _ONTOLOGY_DIR.glob("shapes*.ttl"))

# Findings STE's check() must not raise on ontology prose (the same
# allowance ste.py documents for flavored-mode text).
_BLOCKING_CHECK_RULES = {"present-perfect", "passive-voice", "sentence-length"}


def _ttl_literals() -> list[tuple[str, str, str]]:
    """(file name, predicate local name, literal text) for every literal
    object of a _TEXT_PREDICATES triple across every ontology/model*.ttl
    and ontology/shapes*.ttl file."""
    out: list[tuple[str, str, str]] = []
    for path in _TTL_FILES:
        g = rdflib.Graph()
        g.parse(str(path), format="turtle")
        for s, p, o in g:
            if str(p) not in _TEXT_PREDICATES:
                continue
            if not isinstance(o, rdflib.Literal):
                continue
            pred_local = str(p).rsplit("#", 1)[-1]
            out.append((path.name, pred_local, str(o)))
    return out


def _vocab_strings() -> list[tuple[str, str]]:
    """(vocabulary name, value) for every string in vocab.json — a
    future description-like field lands here automatically."""
    vocab_path = _ONTOLOGY_DIR / "vocab.json"
    data = json.loads(vocab_path.read_text())
    out: list[tuple[str, str]] = []
    for name, values in data.items():
        for value in values:
            out.append((name, value))
    return out


# ---------------------------------------------------------------------
# Every text-bearing literal in the ontology's TTL files normalizes to
# itself (no contraction/semicolon/marketing/filler/nominalisation/
# phrasal-verb fix applies) and carries no blocking check() finding.
# ---------------------------------------------------------------------

def test_every_ontology_ttl_literal_is_already_ste():
    literals = _ttl_literals()
    assert len(literals) > 20, "expected many rdfs:/sh: literals across the ontology files"

    bad_normalize = []
    bad_check = []
    for fname, pred, text in literals:
        fixed, rules = ste.normalize(text, "flavored")
        if rules:
            bad_normalize.append((fname, pred, text, rules))
        for finding in ste.check(text, "flavored"):
            if finding.rule in _BLOCKING_CHECK_RULES:
                bad_check.append((fname, pred, text, finding.rule, finding.message))

    assert not bad_normalize, (
        "these ontology literals are not plain English yet "
        "(normalize would change them): " + repr(bad_normalize))
    assert not bad_check, (
        "these ontology literals fail an STE check() rule: " + repr(bad_check))


# ---------------------------------------------------------------------
# Every string in vocab.json is already plain English too (today these
# are short enum words, so this is a forward guard, not a live finding).
# ---------------------------------------------------------------------

def test_every_vocab_json_string_is_already_ste():
    strings = _vocab_strings()
    assert strings, "vocab.json should not be empty"

    bad_normalize = []
    bad_check = []
    for name, text in strings:
        fixed, rules = ste.normalize(text, "flavored")
        if rules:
            bad_normalize.append((name, text, rules))
        for finding in ste.check(text, "flavored"):
            if finding.rule in _BLOCKING_CHECK_RULES:
                bad_check.append((name, text, finding.rule, finding.message))

    assert not bad_normalize, bad_normalize
    assert not bad_check, bad_check


# ---------------------------------------------------------------------
# vocab.json itself is not stale against vocab.py's own generator (the
# drift test vocab.py's header promises) — this test edits vocab.py
# only if a description-like field is ever added there.
# ---------------------------------------------------------------------

def test_vocab_json_matches_vocab_py_generator():
    from prism_service.ontology import vocab

    on_disk = json.loads((_ONTOLOGY_DIR / "vocab.json").read_text())
    assert on_disk == vocab.build_vocab()
