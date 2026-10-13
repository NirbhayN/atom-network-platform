# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""CERT-5: the generator is the assertion engine run forward.

The property that matters is the ROUND TRIP: whatever `generate_payload`
emits from a coherent row set must grade clean through `evaluate_specs` over
the same rows, and a payload built with `fault=` must fail exactly the
assertion kinds the fault's report names (sibling kinds may honestly co-fail;
the named ones are the contract). Both directions are exercised through the
real `XmlCodec` and the real judge — no mocks, no DB: rows are
`SimpleNamespace`, payloads are strings.

The relativity pin matters most: a mandatory attribute on an OPTIONAL element
must neither drag the element into a mandatory-only payload (generator side)
nor fail validation when the element is absent (judge side —
`_ancestor_absent`). The two sides agreeing on that is the symmetry this
module exists for.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.wire.xml_codec import XmlCodec
from app.services.payload_generator import (
    FaultNotApplicable, generate_payload,
)
from app.services.registry_validation import validate_against_fields

CODEC = XmlCodec()
NS = "urn:atom:test"


def row(xpath: str, **kw) -> SimpleNamespace:
    base = dict(xpath=xpath, is_attribute="@" in xpath.rsplit("/", 1)[-1],
                occurrence=None, mandatory=None, datatype=None,
                length_rule=None, enum_values=None, pattern_rule=None,
                condition_text=None, status="active")
    base.update(kw)
    return SimpleNamespace(**base)


def transfer_fields() -> list[SimpleNamespace]:
    """A registry in miniature — every constraint kind appears at least once,
    plus the optional-element/mandatory-attribute relativity trap (Ext)."""
    return [
        row("ReqTransfer", occurrence="1..1", mandatory="Y"),
        row("ReqTransfer/@xmlns", occurrence="1..1", mandatory="Y"),
        row("ReqTransfer/Head", occurrence="1..1", mandatory="Y"),
        row("ReqTransfer/Head/@ver", occurrence="1..1", mandatory="Y",
            enum_values=["2.0", "2.1"]),
        row("ReqTransfer/Head/@ts", occurrence="1..1", mandatory="Y",
            datatype="DateTime"),
        row("ReqTransfer/Head/@orgId", occurrence="1..1", mandatory="Y",
            pattern_rule=r"[A-Z]{4}\d{2}"),
        row("ReqTransfer/Head/@msgId", occurrence="1..1", mandatory="Y",
            length_rule="Min Length 10 Max Length 35"),
        row("ReqTransfer/Txn", occurrence="1..1", mandatory="Y"),
        row("ReqTransfer/Txn/@id", occurrence="1..1", mandatory="Y",
            length_rule="Length 12"),
        row("ReqTransfer/Txn/@type", occurrence="1..1", mandatory="Y",
            enum_values=["DEBIT", "CREDIT"]),
        row("ReqTransfer/Txn/Amt", occurrence="1..1", mandatory="Y"),
        row("ReqTransfer/Txn/Amt/@value", occurrence="1..1", mandatory="Y",
            datatype="Numeric", length_rule="minInclusive 1 maxInclusive 100000"),
        row("ReqTransfer/Txn/Amt/@curr", occurrence="0..1", mandatory="C",
            condition_text="required for cross-border transfers"),
        row("ReqTransfer/Txn/Remarks", occurrence="0..1", mandatory="N",
            length_rule="Max Length 50"),
        row("ReqTransfer/Txn/Tag", occurrence="0..5", mandatory="N",
            length_rule="Max Length 10"),
        row("ReqTransfer/Txn/Ext", occurrence="0..1", mandatory="N"),
        row("ReqTransfer/Txn/Ext/@code", occurrence="1..1", mandatory="Y"),
    ]


def gen(**kw):
    kw.setdefault("api_name", "ReqTransfer")
    kw.setdefault("namespace", NS)
    kw.setdefault("fields", transfer_fields())
    return generate_payload(**kw)


def judge(xml: str, fields=None) -> dict:
    return validate_against_fields(
        xml, api_name="ReqTransfer", fields=fields or transfer_fields(), codec=CODEC)


def failing(xml: str, fields=None) -> set[tuple[str, str]]:
    return {(v["field"], v["kind"]) for v in judge(xml, fields)["violations"]}


# ── the round trip (the property everything else leans on) ───────────────────

def test_mandatory_payload_round_trips_clean():
    result = gen()
    verdict = judge(result.xml)
    assert verdict["valid"], verdict["violations"]
    assert verdict["summary"]["fail"] == 0
    assert result.report["gaps"] == []


def test_include_all_round_trips_clean():
    result = gen(include="all")
    verdict = judge(result.xml)
    assert verdict["valid"], verdict["violations"]
    doc = CODEC.parse(result.xml)
    assert CODEC.count(doc, "ReqTransfer/Txn/Remarks") == 1
    assert CODEC.count(doc, "ReqTransfer/Txn/Ext/@code") == 1


# ── inclusion ────────────────────────────────────────────────────────────────

def test_mandatory_mode_omits_optionals_and_reports_conditionals():
    result = gen()
    doc = CODEC.parse(result.xml)
    assert CODEC.count(doc, "ReqTransfer/Head") == 1
    assert CODEC.count(doc, "ReqTransfer/Txn/Remarks") == 0
    assert CODEC.count(doc, "ReqTransfer/Txn/Amt/@curr") == 0
    assert "ReqTransfer/Txn/Remarks" in result.report["excluded_optional"]
    conditional = result.report["conditional_excluded"]
    assert [c["path"] for c in conditional] == ["ReqTransfer/Txn/Amt/@curr"]
    assert "cross-border" in conditional[0]["condition"]


def test_mandatory_attr_does_not_drag_its_optional_element_in():
    """The relativity pin, generator side: Ext/@code is mandatory WITHIN Ext,
    and Ext is optional — a mandatory-only payload has neither. The judge
    agrees via its ancestor-absent SKIP, which is why the round trip stays
    clean (see test_mandatory_payload_round_trips_clean)."""
    result = gen()
    doc = CODEC.parse(result.xml)
    assert CODEC.count(doc, "ReqTransfer/Txn/Ext") == 0


def test_valued_conditional_is_included_not_reported():
    result = gen(values={"ReqTransfer/Txn/Amt/@curr": "USD"})
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Txn/Amt/@curr") == ["USD"]
    assert result.report["conditional_excluded"] == []


def test_explicit_value_pulls_the_optional_chain_in():
    result = gen(values={"ReqTransfer/Txn/Ext/@code": "E1"})
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Txn/Ext/@code") == ["E1"]
    assert judge(result.xml)["valid"]


# ── value resolution ─────────────────────────────────────────────────────────

def test_constraint_derived_values_per_kind():
    result = gen()
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Head/@ver") == ["2.0"]        # first enum
    assert CODEC.values(doc, "ReqTransfer/Head/@orgId") == ["AAAA11"]   # from pattern
    assert CODEC.values(doc, "ReqTransfer/Txn/@id") == ["A" * 12]       # exact length
    assert CODEC.values(doc, "ReqTransfer/Txn/Amt/@value") == ["1"]     # minInclusive
    sources = result.report["value_sources"]
    assert sources["ReqTransfer/Head/@ver"] == "enum"
    assert sources["ReqTransfer/Head/@orgId"] == "pattern"
    assert sources["ReqTransfer/Txn/@id"] == "datatype"


def test_case_value_beats_profile_beats_auto():
    result = gen(values={"ReqTransfer/Head/@orgId": "ZZZZ99"},
                 profile={"Head/@orgId": "BBBB22", "Head/@msgId": "MSG-000000001"})
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Head/@orgId") == ["ZZZZ99"]
    assert CODEC.values(doc, "ReqTransfer/Head/@msgId") == ["MSG-000000001"]
    sources = result.report["value_sources"]
    assert sources["ReqTransfer/Head/@orgId"] == "case"
    assert sources["ReqTransfer/Head/@msgId"] == "profile"


def test_profile_provider_entry_computes_at_send_time():
    result = gen(profile={"Head/@msgId": {"provider": "uuid"}},
                 providers={"uuid": lambda: "FIXED00001"})
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Head/@msgId") == ["FIXED00001"]
    assert result.report["value_sources"]["ReqTransfer/Head/@msgId"] == "provider:uuid"
    assert result.report["provider_values"]["ReqTransfer/Head/@msgId"] == "FIXED00001"


def test_unknown_provider_is_a_gap_with_auto_fallback():
    result = gen(profile={"Head/@msgId": {"provider": "nope"}})
    assert any("unknown provider" in g["reason"] for g in result.report["gaps"])
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Head/@msgId") == ["A" * 10]


def test_case_value_violating_its_own_row_is_emitted_but_never_silently():
    """A caller probing on purpose gets the payload AND the honest gap; the
    judge then fails it for the same documented reason."""
    result = gen(values={"ReqTransfer/Txn/@type": "WRONG"})
    assert any("not in enum" in g["reason"] for g in result.report["gaps"])
    assert ("ReqTransfer/Txn/@type", "enum") in failing(result.xml)


def test_case_value_naming_no_field_is_reported():
    result = gen(values={"ReqTransfer/Nope": "x"})
    assert result.report["unresolved_values"] == ["ReqTransfer/Nope"]
    assert judge(result.xml)["valid"]


# ── namespace ────────────────────────────────────────────────────────────────

def test_namespace_binding_satisfies_the_xmlns_row():
    result = gen()
    assert f'xmlns="{NS}"' in result.xml
    doc = CODEC.parse(result.xml)
    assert CODEC.count(doc, "ReqTransfer/@xmlns") == 1
    assert CODEC.values(doc, "ReqTransfer/@xmlns") == [NS]


# ── repeats ──────────────────────────────────────────────────────────────────

def _counter(prefix: str):
    it = iter(range(1, 100))
    return lambda: f"{prefix}{next(it)}"


def test_repeats_clone_within_occurrence_and_recompute_providers():
    result = gen(profile={"Txn/Tag": {"provider": "seq"}},
                 providers={"seq": _counter("T")},
                 repeats={"ReqTransfer/Txn/Tag": 3})
    doc = CODEC.parse(result.xml)
    assert list(CODEC.values(doc, "ReqTransfer/Txn/Tag")) == ["T1", "T2", "T3"]
    assert result.report["gaps"] == []
    assert judge(result.xml)["valid"]


def test_repeats_beyond_occurrence_maximum_is_a_gap_not_a_silent_clamp():
    result = gen(values={"ReqTransfer/Txn/Tag": "T"},
                 repeats={"ReqTransfer/Txn/Tag": 7})
    assert any("exceed occurrence maximum" in g["reason"]
               for g in result.report["gaps"])
    assert ("ReqTransfer/Txn/Tag", "occurrence") in failing(result.xml)


def test_repeat_target_absent_from_payload_is_a_gap():
    result = gen(repeats={"ReqTransfer/Txn/Remarks": 2})
    assert any(g["path"] == "ReqTransfer/Txn/Remarks" and "absent" in g["reason"]
               for g in result.report["gaps"])


# ── faults (each must trip exactly its declared assertion kinds) ─────────────

@pytest.mark.parametrize("fault,expected", [
    ({"path": "ReqTransfer/Head/@msgId", "kind": "missing"},
     {("ReqTransfer/Head/@msgId", "mandatory"), ("ReqTransfer/Head/@msgId", "occurrence")}),
    ({"path": "ReqTransfer/Txn/@type", "kind": "enum"},
     {("ReqTransfer/Txn/@type", "enum")}),
    ({"path": "ReqTransfer/Head/@orgId", "kind": "pattern"},
     {("ReqTransfer/Head/@orgId", "pattern")}),
    ({"path": "ReqTransfer/Head/@msgId", "kind": "length_under"},
     {("ReqTransfer/Head/@msgId", "length")}),
    ({"path": "ReqTransfer/Txn/@id", "kind": "length_over"},
     {("ReqTransfer/Txn/@id", "length")}),
    ({"path": "ReqTransfer/Txn/Amt/@value", "kind": "datatype"},
     {("ReqTransfer/Txn/Amt/@value", "datatype")}),
    ({"path": "ReqTransfer/Txn/Tag", "kind": "occurrence_extra"},
     {("ReqTransfer/Txn/Tag", "occurrence")}),
])
def test_fault_trips_its_declared_assertions(fault, expected):
    result = gen(fault=fault)
    declared = {(fault["path"], kind)
                for kind in result.report["fault"]["expected_failure"]}
    assert declared == expected
    assert expected <= failing(result.xml), \
        f"declared failures did not all fire: {judge(result.xml)['violations']}"
    # the positive twin is clean — the fault is the ONLY difference that matters
    assert judge(gen().xml)["valid"]


def test_fault_on_a_bounded_numeric_exceeds_max_inclusive():
    result = gen(fault={"path": "ReqTransfer/Txn/Amt/@value", "kind": "length_over"})
    doc = CODEC.parse(result.xml)
    assert CODEC.values(doc, "ReqTransfer/Txn/Amt/@value") == ["100001"]
    assert ("ReqTransfer/Txn/Amt/@value", "length") in failing(result.xml)


@pytest.mark.parametrize("fault,reason_bit", [
    ({"path": "ReqTransfer/Txn/Remarks", "kind": "enum"}, "no enum"),
    ({"path": "ReqTransfer/Txn/Remarks", "kind": "missing"}, "not required"),
    ({"path": "ReqTransfer/Head/@ts", "kind": "datatype"}, "not machine-checked"),
    ({"path": "ReqTransfer/Nowhere", "kind": "enum"}, "no registry field"),
    ({"path": "ReqTransfer/Head", "kind": "bogus"}, "unknown fault kind"),
])
def test_unconstructible_faults_refuse_instead_of_testing_nothing(fault, reason_bit):
    with pytest.raises(FaultNotApplicable, match=reason_bit):
        gen(fault=fault)
