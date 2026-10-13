# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""TSD registry scoping — full tag tables only for the feature's APIs.

The dangerous mutant is the ALL-API DUMP: the post-write sweep used to expand
every registry API the prose merely mentioned into a full spec section, so a
TSD that name-dropped reused APIs in its Interfaces-in-Scope inventory read as
a whole-platform API reference. These tests pin the new contract:

  * core (ratified) APIs      → full "API Specification — <Name>" sections
  * mentioned-but-not-core    → one "<Name>: reused unchanged" cross-reference
  * no core at all (degraded) → mentions render in full, no cross-references
    (a thin document with real tables beats one with none)

and that the cross-reference phrase stays in lockstep with the deterministic
eval check that accepts it in place of a full section.
"""
from __future__ import annotations

from app.services.api_registry_ingest import (
    REFERENCED_API_MARKER,
    complete_message_pairs,
    render_registry_reference_section,
    select_registry_api_scope,
)

AVAILABLE = {"ReqDelegateAdd": {}, "RespDelegateAdd": {}, "ReqPay": {}, "RespPay": {}}


def test_core_apis_render_full_and_mentions_become_references():
    full, referenced = select_registry_api_scope(
        core=["ReqDelegateAdd", "RespDelegateAdd"],
        mentioned=["ReqDelegateAdd", "ReqPay", "RespPay"],
        available=AVAILABLE,
    )
    assert full == ["ReqDelegateAdd", "RespDelegateAdd"]
    assert referenced == ["ReqPay", "RespPay"]


def test_core_names_missing_from_registry_are_dropped():
    full, referenced = select_registry_api_scope(
        core=["ReqDelegateAdd", "ReqNotInRegistry"],
        mentioned=[], available=AVAILABLE,
    )
    assert full == ["ReqDelegateAdd"]
    assert referenced == []


def test_no_core_falls_back_to_full_sections_for_mentions():
    full, referenced = select_registry_api_scope(
        core=[], mentioned=["ReqPay", "ReqUnknown"], available=AVAILABLE,
    )
    assert full == ["ReqPay"]
    assert referenced == []


def test_all_core_missing_from_registry_behaves_as_no_core():
    full, referenced = select_registry_api_scope(
        core=["ReqGhost"], mentioned=["ReqPay"], available=AVAILABLE,
    )
    assert full == ["ReqPay"]
    assert referenced == []


def test_reference_section_shape_and_eval_marker():
    section = render_registry_reference_section(["ReqPay", "RespPay"])
    assert section["section_key"] == "api_registry_referenced"
    assert "table_data" not in section
    text = "\n".join(section["paragraphs"])
    # The exact phrase the deterministic eval check
    # (check_tsd_api_specs_registry_backed) accepts in place of a full
    # "API Specification — <Name>" section.
    assert f"ReqPay: {REFERENCED_API_MARKER}" in text
    assert f"RespPay: {REFERENCED_API_MARKER}" in text


def test_reference_section_empty_names_renders_nothing():
    assert render_registry_reference_section([]) is None


def test_flow_with_req_only_pulls_in_resp_counterparts():
    # The user-facing guarantee: a flow using ReqA + ReqB yields full sections
    # for ALL FOUR of ReqA/RespA/ReqB/RespB, even when the flow spec never
    # spells out the Resp halves.
    available = {"ReqA": {}, "RespA": {}, "ReqB": {}, "RespB": {}, "ReqC": {}}
    core = complete_message_pairs(["ReqA", "ReqB"], available)
    assert core == ["ReqA", "ReqB", "RespA", "RespB"]
    full, referenced = select_registry_api_scope(core, mentioned=["ReqC"],
                                                 available=available)
    assert full == ["ReqA", "ReqB", "RespA", "RespB"]
    assert referenced == ["ReqC"]


def test_pair_completion_works_resp_to_req_and_skips_unregistered():
    available = {"ReqA": {}, "RespA": {}, "RespOrphan": {}}
    assert complete_message_pairs(["RespA"], available) == ["RespA", "ReqA"]
    # No ReqOrphan in the registry — nothing invented.
    assert complete_message_pairs(["RespOrphan"], available) == ["RespOrphan"]
    # Non-Req/Resp names (other domains, Ack) pass through untouched.
    assert complete_message_pairs(["Ack"], available) == ["Ack"]


def test_pair_completion_no_duplicates():
    available = {"ReqA": {}, "RespA": {}}
    assert complete_message_pairs(["ReqA", "RespA"], available) == ["ReqA", "RespA"]


def test_eval_check_accepts_cross_reference_in_place_of_section():
    # The check's textual contract, exercised without a DB: a mentioned API
    # passes with EITHER surface. Mirrors the string literals in
    # evaluation/deterministic.check_tsd_api_specs_registry_backed.
    doc_with_section = "... API Specification — ReqPay ..."
    doc_with_reference = "\n".join(
        render_registry_reference_section(["ReqPay"])["paragraphs"])
    for text in (doc_with_section, doc_with_reference):
        assert ("API Specification — ReqPay" in text
                or "ReqPay: reused unchanged" in text)
