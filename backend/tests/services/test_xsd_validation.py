# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Deterministic XML-block validation for generated docs (accuracy S6).

Covers the two-way split the module exists to get right: SCHEMA blocks must REPRODUCE
an approved schema (whitespace-normalised substring), INSTANCE blocks must VALIDATE
against the approved set. Both paths fail-open.
"""
import pytest
from app.services import xsd_validation as V


def test_extract_xml_blocks_case_insensitive_and_drops_empty():
    md = "intro\n```xml\n<a/>\n```\nmid\n```XML\n<b/>\n```\n```xml\n\n```\n"
    assert V.extract_xml_blocks(md) == ["<a/>", "<b/>"]
    assert V.extract_xml_blocks("") == []
    assert V.extract_xml_blocks("no fenced blocks here") == []


def test_is_schema_block():
    assert V._is_schema_block('<xs:schema xmlns:xs="...">')
    assert V._is_schema_block('<xsd:schema>')
    assert V._is_schema_block('<schema xmlns="http://www.w3.org/2001/XMLSchema">')
    assert not V._is_schema_block('<ReqTransfer><Txn/></ReqTransfer>')


def test_no_approved_schemas_yields_no_findings():
    md = "```xml\n<xs:schema><xs:element name='X'/></xs:schema>\n```"
    assert V.validate_xml_blocks(md, []) == []                # empty approved set → early return
    assert V.validate_xml_blocks("", ["<xs:schema/>"]) == []  # no blocks → nothing to check


def test_schema_block_matching_approved_is_clean():
    # collapsed-whitespace match: the approved schema has double spaces, the block single —
    # both normalise to the same string, so the block is recognised as a reproduction.
    approved = '<xs:schema  xmlns:xs="x"><xs:element  name="Refund"/></xs:schema>'
    md = '```xml\n<xs:schema xmlns:xs="x"><xs:element name="Refund"/></xs:schema>\n```'
    assert V.validate_xml_blocks(md, [approved]) == []


def test_schema_block_not_in_approved_is_flagged():
    approved = '<xs:schema xmlns:xs="x"><xs:element name="Refund"/></xs:schema>'
    md = '```xml\n<xs:schema xmlns:xs="x"><xs:element name="Invented"/></xs:schema>\n```'
    findings = V.validate_xml_blocks(md, [approved])
    assert len(findings) == 1 and "SCHEMA that does not match" in findings[0]


def test_instance_block_validates_against_schema():
    pytest.importorskip("xmlschema")
    schema = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
              '<xs:element name="Root"><xs:complexType><xs:sequence>'
              '<xs:element name="Child" type="xs:string"/>'
              '</xs:sequence></xs:complexType></xs:element></xs:schema>')
    assert V.validate_xml_blocks("```xml\n<Root><Child>ok</Child></Root>\n```", [schema]) == []
    findings = V.validate_xml_blocks("```xml\n<Root><Nope/></Root>\n```", [schema])
    assert len(findings) == 1 and "does not validate" in findings[0]


def test_malformed_instance_is_not_double_reported():
    pytest.importorskip("xmlschema")
    schema = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
              '<xs:element name="Root"/></xs:schema>')
    # not well-formed → fail-open, no finding from the instance path
    assert V.validate_xml_blocks("```xml\n<Root><unclosed>\n```", [schema]) == []


# ── compile_generated_schemas (B2): deterministic compile gate for generated docs ──

from app.services.xsd_validation import compile_generated_schemas


def _doc(*files):
    """Markdown doc in the generator's format: per-file heading + ```xml block."""
    parts = []
    for fname, xml in files:
        parts.append(f"### `{fname}`\n\n```xml\n{xml}\n```")
    return "\n\n".join(parts)


_OK_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:t" xmlns="urn:t" elementFormDefault="qualified">
  <xs:element name="Amount" type="xs:decimal"/>
</xs:schema>"""


def test_clean_schema_compiles_with_no_errors():
    assert compile_generated_schemas(_doc(("amount.xsd", _OK_SCHEMA))) == []


def test_no_xml_blocks_is_clean():
    assert compile_generated_schemas("just prose, no schemas") == []
    assert compile_generated_schemas("") == []


def test_malformed_xml_is_reported():
    errs = compile_generated_schemas(_doc(("broken.xsd", "<xs:schema><unclosed>")))
    assert errs and "broken.xsd" in errs[0] and "well-formed" in errs[0]


def test_non_schema_root_is_reported():
    errs = compile_generated_schemas(_doc(("msg.xsd", "<Envelope><Body/></Envelope>")))
    assert errs and "not an xs:schema" in errs[0]


def test_invalid_facet_pattern_is_reported():
    bad = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:t">
  <xs:simpleType name="Code">
    <xs:restriction base="xs:string"><xs:pattern value="[A-Z"/></xs:restriction>
  </xs:simpleType>
</xs:schema>"""
    errs = compile_generated_schemas(_doc(("code.xsd", bad)))
    assert errs and "code.xsd" in errs[0]


def test_sibling_include_resolves_inside_the_document():
    # b.xsd includes a.xsd — both generated in the SAME doc: the sandbox materializes
    # them under their filenames, so the include resolves and the type ref is checked.
    a = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:t" xmlns="urn:t">
  <xs:simpleType name="Iban">
    <xs:restriction base="xs:string"><xs:pattern value="[A-Z]{2}[0-9A-Z]+"/></xs:restriction>
  </xs:simpleType>
</xs:schema>"""
    b = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:t" xmlns="urn:t">
  <xs:include schemaLocation="a.xsd"/>
  <xs:element name="Account" type="Iban"/>
</xs:schema>"""
    assert compile_generated_schemas(_doc(("a.xsd", a), ("b.xsd", b))) == []


def test_external_base_schema_reference_is_not_a_defect():
    # Importing an ecosystem base schema that is NOT part of this document must not
    # fail the gate — nor should the unknown-type fallout it causes.
    ext = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:t" xmlns="urn:t" xmlns:base="urn:base">
  <xs:import namespace="urn:base" schemaLocation="ecosystem_base.xsd"/>
  <xs:element name="Wrapped" type="base:CoreType"/>
</xs:schema>"""
    assert compile_generated_schemas(_doc(("wrap.xsd", ext))) == []


def test_mixed_doc_reports_only_the_broken_file():
    errs = compile_generated_schemas(_doc(("good.xsd", _OK_SCHEMA),
                                          ("bad.xsd", "<xs:schema><oops>")))
    assert len(errs) == 1 and errs[0].startswith("bad.xsd")


# ── B5: extract_xsd_blocks attribution scoping ──────────────────────────────────────

def test_extract_scopes_filename_to_the_inter_block_region():
    from app.services.xsd_bundle import extract_xsd_blocks
    md = ("### `first.xsd`\n```xml\n<a/>\n```\n"
          "This section discusses `mentioned.xsd` consumers at length.\n"
          "### `second.xsd`\n```xml\n<b/>\n```\n")
    blocks = extract_xsd_blocks(md)
    assert [f for f, _ in blocks] == ["first.xsd", "second.xsd"]


def test_extract_unnamed_block_gets_positional_name():
    from app.services.xsd_bundle import extract_xsd_blocks
    blocks = extract_xsd_blocks("intro\n```xml\n<a/>\n```")
    assert blocks == [("xsd_part_1.xsd", "<a/>")]


def test_extract_reemitted_file_last_body_wins():
    from app.services.xsd_bundle import extract_xsd_blocks
    md = ("### `dup.xsd`\n```xml\n<old/>\n```\n"
          "### `other.xsd`\n```xml\n<o/>\n```\n"
          "Corrected version:\n### `dup.xsd`\n```xml\n<fixed/>\n```\n")
    blocks = extract_xsd_blocks(md)
    assert blocks == [("dup.xsd", "<fixed/>"), ("other.xsd", "<o/>")]
