# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""CERT-5: registry-direct validation — the judge with no pack in between.

The dangerous mutant here is the ALL-SKIP PASS: `evaluate_specs` skips every
field spec when the parsed root matches no spec path (the C-3 protection),
so a wrong-message payload judged naively would come back "valid" with zero
failures. These tests pin that every such case — wrong root, case slip,
malformed body, unknown API — is an explicit named error, never a pass.

`validate_against_fields` is pure (SimpleNamespace rows, string payloads);
`validate_payload` gets a real SQLite session over the two registry tables
to pin the root→message mapping.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.registry_validation import (
    validate_against_fields, validate_payload, xsd_gate,
)


def row(xpath: str, **kw) -> SimpleNamespace:
    base = dict(xpath=xpath, occurrence=None, mandatory=None, datatype=None,
                length_rule=None, enum_values=None, pattern_rule=None,
                condition_text=None, status="active")
    base.update(kw)
    return SimpleNamespace(**base)


FIELDS = [
    row("ReqPing", occurrence="1..1", mandatory="Y"),
    row("ReqPing/@id", occurrence="1..1", mandatory="Y", length_rule="Length 4"),
    row("ReqPing/@mode", occurrence="0..1", mandatory="N",
        enum_values=["FULL", "LITE"]),
]


# ── validate_against_fields (pure) ───────────────────────────────────────────

def test_valid_payload_passes_with_a_summary():
    verdict = validate_against_fields('<ReqPing id="ABCD" mode="FULL"/>',
                                      api_name="ReqPing", fields=FIELDS)
    assert verdict["valid"]
    assert verdict["violations"] == []
    assert verdict["summary"]["fail"] == 0
    assert verdict["summary"]["pass"] > 0


def test_violations_name_the_field_and_the_kind():
    verdict = validate_against_fields('<ReqPing id="TOOLONG" mode="TURBO"/>',
                                      api_name="ReqPing", fields=FIELDS)
    assert not verdict["valid"]
    got = {(v["field"], v["kind"]) for v in verdict["violations"]}
    assert ("ReqPing/@id", "length") in got
    assert ("ReqPing/@mode", "enum") in got


def test_skips_travel_with_their_reasons():
    verdict = validate_against_fields('<ReqPing id="ABCD"/>',
                                      api_name="ReqPing", fields=FIELDS)
    assert verdict["valid"]
    assert any(s["field"] == "ReqPing/@mode" and s["reason"]
               for s in verdict["skips"])


def test_wrong_root_is_an_error_not_an_all_skip_pass():
    verdict = validate_against_fields('<RespPing id="ABCD"/>',
                                      api_name="ReqPing", fields=FIELDS)
    assert verdict["valid"] is False
    assert verdict["error"] == "root_mismatch"


def test_root_case_slip_is_named_as_such():
    verdict = validate_against_fields('<reqping id="ABCD"/>',
                                      api_name="ReqPing", fields=FIELDS)
    assert verdict["valid"] is False
    assert verdict["error"] == "root_mismatch"
    assert "case" in verdict["detail"]


def test_malformed_xml_is_an_error():
    verdict = validate_against_fields("<oops", api_name="ReqPing", fields=FIELDS)
    assert verdict["valid"] is False
    assert verdict["error"] == "malformed_xml"


def test_constraint_free_message_says_so():
    verdict = validate_against_fields("<ReqPing/>", api_name="ReqPing",
                                      fields=[row("ReqPing"), row("ReqPing/@id")])
    assert verdict["valid"]
    assert verdict["note"] == "message declares no checkable constraints"


# ── validate_payload (root→message mapping over a real session) ──────────────

@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.api_registry import ApiField, ApiMessage

    engine = create_engine("sqlite:///:memory:")
    ApiMessage.__table__.create(engine)
    ApiField.__table__.create(engine)
    session = sessionmaker(bind=engine)()

    msg = ApiMessage(api_name="ReqPing", direction="request")
    session.add(msg)
    session.flush()
    session.add_all([
        ApiField(message_id=msg.id, position=0, depth=0, xml_tag="ReqPing",
                 xpath="ReqPing", occurrence="1..1", mandatory="Y"),
        ApiField(message_id=msg.id, position=1, depth=1, xml_tag="id",
                 is_attribute=True, xpath="ReqPing/@id",
                 occurrence="1..1", mandatory="Y", length_rule="Length 4"),
    ])
    session.commit()
    yield session
    session.close()


def test_payload_maps_to_its_message_by_root_element(db):
    verdict = validate_payload(db, '<ReqPing id="ABCD"/>')
    assert verdict["valid"]
    assert verdict["api_name"] == "ReqPing"
    assert verdict["matched_by"] == "root_element"

    verdict = validate_payload(db, "<ReqPing/>")
    assert not verdict["valid"]
    assert {(v["field"], v["kind"]) for v in verdict["violations"]} == {
        ("ReqPing/@id", "mandatory"), ("ReqPing/@id", "occurrence")}


def test_unknown_root_is_named(db):
    verdict = validate_payload(db, "<ReqNope/>")
    assert verdict["valid"] is False
    assert verdict["error"] == "unknown_api"
    assert verdict["root"] == "ReqNope"


def test_case_slip_finds_the_message_then_reports_the_slip(db):
    """Lookup is case-insensitive so the answer is 'your case is wrong',
    not the useless 'unknown API'."""
    verdict = validate_payload(db, '<reqping id="ABCD"/>')
    assert verdict["valid"] is False
    assert verdict["error"] == "root_mismatch"


def test_explicit_pin_refuses_a_disagreeing_root(db):
    verdict = validate_payload(db, "<ReqOther/>", api_name="ReqPing")
    assert verdict["valid"] is False
    assert verdict["error"] == "root_mismatch"
    # the refusal still says HOW the message was picked — useful to the caller
    assert verdict["matched_by"] == "explicit"


# ── xsd_gate ─────────────────────────────────────────────────────────────────

def test_gate_skips_honestly_without_a_schema(tmp_path):
    assert xsd_gate("<ReqPing/>", None)["status"] == "skipped"
    assert xsd_gate("<ReqPing/>", str(tmp_path / "gone.xsd"))["status"] == "skipped"


def test_gate_validates_against_a_real_schema(tmp_path):
    pytest.importorskip("xmlschema")
    schema = tmp_path / "ping.xsd"
    schema.write_text(
        '<?xml version="1.0"?>'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="Ping"><xs:complexType>'
        '<xs:attribute name="id" use="required"/>'
        '</xs:complexType></xs:element></xs:schema>')
    assert xsd_gate('<Ping id="1"/>', str(schema))["status"] == "passed"
    failed = xsd_gate("<Ping/>", str(schema))
    assert failed["status"] == "failed"
    assert failed["errors"]
