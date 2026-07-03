"""Tests for the reverse-engineering interview state machine.

The guarantees: it converges to a stable/complete spec, NEVER reaches
'ready' with a blocking gap, absorbs a transient dropped rule (re-asks),
and serializes so the session lives in PRISM memory."""

from __future__ import annotations

import copy

from prism_service.services import magic_interview as mi

THIN = {"db": "clinic", "module": "clinic", "entities": [
    {"name": "appointments", "fields": [
        {"name": "patient_id", "type": "INTEGER"},
        {"name": "status", "type": "TEXT"}],
     "rules": [{"type": "fk", "field": "patient_id", "ref": "patients"}]}]}


def _refine_good(spec, answered):
    """Deterministic stand-in for the SLM: folds answers into the spec."""
    s = copy.deepcopy(spec)
    txt = " ".join(a["answer"] for a in answered).lower()
    names = {e["name"] for e in s["entities"]}
    if "patient" in txt and "patients" not in names:
        s["entities"].append({"name": "patients",
                              "fields": [{"name": "name", "type": "TEXT"}]})
    if "scheduled" in txt:
        for e in s["entities"]:
            if e["name"] == "appointments" and not any(
                    r["type"] == "enum" for r in e.get("rules", [])):
                e.setdefault("rules", []).append(
                    {"type": "enum", "field": "status",
                     "values": ["scheduled", "completed", "cancelled"]})
    return s


def _answers(questions):
    # customer answers: give the patient + status facts, decline the rest
    return [{"question": q, "answer":
             ("Patients have a name." if "patient" in q.lower() or "table" in q.lower()
              else "scheduled, completed, cancelled" if "status" in q.lower()
              else "no, not needed")} for q in questions]


def test_converges_to_ready_no_blocking():
    iv = mi.run(copy.deepcopy(THIN), _refine_good, _answers, max_rounds=6)
    assert iv.state == mi.READY
    # the guarantee: never ready with a blocking gap left
    from prism_service.services import magic_spec_gaps as g
    assert not g.check_spec(iv.spec)["blocking"]
    assert any(e["name"] == "patients" for e in iv.spec["entities"])


def test_never_ready_when_blocking_persists():
    # SLM that NEVER adds the patients entity -> blocking never resolves
    def refine_broken(spec, answered):
        return copy.deepcopy(spec)
    iv = mi.run(copy.deepcopy(THIN), refine_broken, _answers, max_rounds=3)
    assert iv.state == mi.EXHAUSTED   # escalated, NOT falsely ready
    assert iv.state != mi.READY


def test_absorbs_transient_dropped_rule():
    # SLM drops patients on round 1, then behaves -> interview re-asks + recovers
    calls = {"n": 0}

    def refine_flaky(spec, answered):
        calls["n"] += 1
        if calls["n"] == 1:
            return copy.deepcopy(spec)          # wobble: adds nothing
        return _refine_good(spec, answered)     # recovers
    iv = mi.run(copy.deepcopy(THIN), refine_flaky, _answers, max_rounds=6)
    assert iv.state == mi.READY
    assert iv.round >= 2   # it took an extra round to absorb the drop


def test_serialize_roundtrip_persists_session():
    iv = mi.SpecInterview(copy.deepcopy(THIN))
    d = iv.to_dict()
    import json
    d2 = json.loads(json.dumps(d))          # survives a memory round-trip
    iv2 = mi.SpecInterview.from_dict(d2)
    assert iv2.state == iv.state
    assert iv2.questions() == iv.questions()
    assert iv2.spec == iv.spec


def test_save_load_session_roundtrip(tmp_path):
    iv = mi.SpecInterview(copy.deepcopy(THIN))
    mi.save_session("acme", iv.to_dict(), data_dir=tmp_path)
    loaded = mi.load_session("acme", data_dir=tmp_path)
    assert loaded is not None
    iv2 = mi.SpecInterview.from_dict(loaded)
    assert iv2.state == iv.state and iv2.questions() == iv.questions()
    # a customer who never started has no session
    assert mi.load_session("nobody", data_dir=tmp_path) is None


def test_business_facts_capture_domain_entities_and_qa():
    iv = mi.run(copy.deepcopy(THIN), _refine_good, _answers, max_rounds=6)
    facts = mi.business_facts(iv)
    texts = " ".join(f["text"] for f in facts)
    assert "clinic" in texts.lower()
    assert "appointments" in texts and "patients" in texts
    assert any(f["name"] == "clarification" for f in facts)


def test_record_facts_writes_durable_memory(tmp_path):
    iv = mi.run(copy.deepcopy(THIN), _refine_good, _answers, max_rounds=6)
    path = mi.record_facts("acme", mi.business_facts(iv), data_dir=tmp_path)
    from pathlib import Path
    body = Path(path).read_text(encoding="utf-8")
    assert "Business knowledge" in body and "clinic" in body.lower()


def test_finalize_build_writes_code_facts_and_endpoints(tmp_path, monkeypatch):
    from prism_service.services import magic_app_builder as ab
    import prism_service.engines.brain_engine as be
    monkeypatch.setattr(ab, "deploy_app", lambda spec, **k: None)  # no network

    class _FakeBrain:
        def __init__(self, **k):
            pass
        def ingest(self, paths):
            return 3
    monkeypatch.setattr(be, "Brain", _FakeBrain)

    spec = {"db": "shop", "module": "shop", "entities": [
        {"name": "customers", "fields": [{"name": "name", "type": "TEXT"}]}]}
    r = mi.finalize_build("acme", spec, data_dir=tmp_path,
                          facts=[{"name": "x", "text": "a shop"}])
    src = tmp_path / "projects" / "acme" / "magic_app"
    assert (src / "customers.get.hl").exists()       # their CODE artifact
    assert (src / "business-facts.md").exists()       # their memory artifact
    assert r["brain_docs"] == 3                        # ingested into their BRAIN
    assert any("customers" in e for e in r["endpoints"])
