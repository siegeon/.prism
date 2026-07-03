"""Tests for the keyless per-industry design brain (BM25 -> --app-* tokens).

Guarantees: distinct industries yield distinct brand colors; freeform business
text routes to the right vertical via BM25; every result carries all 9 --app-*
keys; resolution is deterministic and never model/network-backed."""

from __future__ import annotations

from prism_service.services import design_intel as di

_TOKEN_KEYS = {"--app-brand", "--app-accent", "--app-bg", "--app-surface",
               "--app-fg", "--app-muted", "--app-border", "--app-radius",
               "--app-font"}


def test_clinic_and_shop_differ():
    clinic = di.design_tokens("clinic")
    shop = di.design_tokens("shop")
    assert clinic["--app-brand"] != shop["--app-brand"]  # each vertical its own look


def test_freeform_fitness_resolves_to_gym_palette():
    gym = di.design_tokens("gym")
    got = di.design_tokens("a boutique fitness studio")
    assert got["--app-brand"] == gym["--app-brand"]      # BM25 hits fitness, not shop


def test_domain_fact_sentence_routes():
    # the exact shape magic_interview business_facts produces
    got = di.design_tokens("This customer's business is a clinic.")
    assert got == di.design_tokens("clinic")


def test_every_token_set_has_all_nine_keys():
    for key in di.industries():
        assert set(di.design_tokens(key)) == _TOKEN_KEYS
    assert set(di.design_tokens("a boutique fitness studio")) == _TOKEN_KEYS
    assert set(di.design_tokens("")) == _TOKEN_KEYS       # generic fallback


def test_no_signal_falls_back_to_generic():
    assert di.design_tokens("") == di.design_tokens("generic")
    assert di.design_tokens("zzzz qqqq") == di.design_tokens("generic")


def test_deterministic_across_calls():
    for q in ("clinic", "a boutique fitness studio", "law firm", ""):
        assert di.design_tokens(q) == di.design_tokens(q)


def test_brand_colors_are_hex():
    for key in di.industries():
        assert di.design_tokens(key)["--app-brand"].startswith("#")


def test_serious_verticals_are_not_purple():
    # anti-pattern guard: clinic/law/finance/logistics must not ship the
    # generic AI purple-gradient look (leading '#8'/'#7'/'#6' violet family).
    for key in ("clinic", "law", "finance", "logistics"):
        brand = di.design_tokens(key)["--app-brand"].lower()
        assert brand[:2] not in ("#8", "#9"), f"{key} brand {brand} looks purple"
