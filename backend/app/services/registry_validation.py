# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Registry-direct payload validation (CERT-5) — the same engine, no pack.

The simulator validates ingress against a PUBLISHED pack's field table
(`simulator/validation.py`), which is right for certification runs: a pack is
a point-in-time contract both sides can name. But partner integration work
needs the question answered directly — "is this XML valid against the live
registry, and if not, which fields are wrong?" — without publishing anything
first. This module answers it from `ApiMessage`/`ApiField` rows.

NOT a third rule engine. Specs are synthesized with the case builder's own
kind→columns mapping (`cert_case_builder._constraints_of` — the same copy the
grader stores) and judged by `cert_assertions.evaluate_specs`, so a payload
that fails here fails at the simulator and at the grader for the same
documented reason.

THE ALL-SKIP TRAP, and why the root is checked up front: `evaluate_specs`
SKIPs every field spec when no parsed document matches the path's root — the
C-3 rule that protects a partner from being failed for OUR missing capture.
Here the payload is present by definition, so a root that matches no spec
path would turn "wrong message entirely" into "zero failures" and read as
valid. The root checks below (exact case included — wire XML is case-
sensitive) turn that into an explicit error instead.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from sqlalchemy import func

from app.adapters.wire.xml_codec import XmlCodec
from app.core.wire.codec import CodecError
from app.services.cert_assertions import (
    FAIL, PASS, SKIP, assertion_failures, evaluate_specs,
)
# The one kind→columns mapping (see that module's `_FIELD_ASSERTIONS`) —
# importing the private helper beats a third copy that drifts.
from app.services.cert_case_builder import _constraints_of

__all__ = ["validate_against_fields", "validate_payload", "xsd_gate"]


def _specs_from_rows(fields: Sequence[Any]) -> list[Any]:
    return [
        SimpleNamespace(assertion_kind=kind, expected=payload, field_path=f.xpath)
        for f in fields
        for kind, payload in _constraints_of(f)
    ]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validate_against_fields(xml_text: str | bytes, *, api_name: str,
                            fields: Sequence[Any],
                            codec: Any | None = None) -> dict:
    """Judge one payload against registry field rows (duck-typed: `.xpath`
    plus the six constraint columns). Pure — no DB.

    Returns ``{"valid", "api_name", "summary", "violations", "skips"}``;
    `violations` is the `assertion_failures` shape the simulator's 422 and the
    partner defect notices already speak. Malformed XML and a root that is not
    `api_name` come back as ``{"valid": False, "error": ...}`` — never as an
    all-SKIP pass (see module docstring)."""
    codec = codec or XmlCodec()
    try:
        doc = codec.parse(xml_text)
    except CodecError as exc:
        return {"valid": False, "error": "malformed_xml", "detail": str(exc)}
    root = _local(doc.tag)
    if root != api_name:
        return {"valid": False, "error": "root_mismatch",
                "detail": f"payload root is <{root}>, expected <{api_name}> "
                          "(exact case — wire XML is case-sensitive)"}

    specs = _specs_from_rows(fields)
    outcomes = evaluate_specs(specs, request_body=xml_text, response_body=None,
                              actual_code=None, codec=codec)
    violations = assertion_failures(outcomes)
    summary = {
        "pass": sum(1 for o in outcomes if o.status == PASS),
        "fail": sum(1 for o in outcomes if o.status == FAIL),
        "skip": sum(1 for o in outcomes if o.status == SKIP),
    }
    out = {
        "valid": not violations,
        "api_name": api_name,
        "summary": summary,
        "violations": violations,
        # SKIPs travel too: "valid" backed by 40 skips and 2 passes is a very
        # different statement from 42 passes, and the caller deserves to see it.
        "skips": [{"field": o.field_path, "kind": o.kind, "reason": o.reason}
                  for o in outcomes if o.status == SKIP],
    }
    if not specs:
        out["note"] = "message declares no checkable constraints"
    return out


def validate_payload(db: Any, xml_text: str | bytes, *,
                     api_name: str | None = None) -> dict:
    """Map a raw payload to a registry message and judge it.

    Mapping is by root element local-name — the same rule the simulator's
    `_identify` applies — case-insensitively for the LOOKUP (so a case slip
    is reported as a case slip, not as "unknown API"), then exact-case for
    the verdict. An explicit `api_name` pins the message instead; a payload
    whose root disagrees with the pin is refused rather than all-SKIP-passed.
    """
    from app.models.api_registry import ApiField, ApiMessage

    codec = XmlCodec()
    try:
        doc = codec.parse(xml_text)
    except CodecError as exc:
        return {"valid": False, "error": "malformed_xml", "detail": str(exc)}
    root = _local(doc.tag)

    wanted = api_name or root
    msg = (db.query(ApiMessage)
           .filter(func.lower(ApiMessage.api_name) == wanted.lower(),
                   ApiMessage.status == "active")
           .first())
    if msg is None:
        return {"valid": False, "error": "unknown_api",
                "detail": f"no active registry message named {wanted!r}",
                "root": root}

    fields = (db.query(ApiField)
              .filter(ApiField.message_id == msg.id, ApiField.status == "active")
              .order_by(ApiField.position)
              .all())
    result = validate_against_fields(xml_text, api_name=msg.api_name,
                                     fields=fields, codec=codec)
    result["matched_by"] = "explicit" if api_name else "root_element"
    return result


def xsd_gate(xml_text: str, schema_path: str | None) -> dict:
    """Validate a payload against its message's SOURCE `.xsd` — the pre-send
    self-check for what the flattened registry rows cannot express
    (xs:choice, sibling order, elementFormDefault).

    Honest degradation over false confidence: schemas live outside the repo
    (`knowledge_base/existing_xsds`, workspace clones), so an absent file or
    library is a named ``skipped``, never a silent pass."""
    if not schema_path:
        return {"status": "skipped", "reason": "message records no source schema"}
    path = Path(schema_path)
    if not path.is_file():
        return {"status": "skipped",
                "reason": f"schema file not available at {schema_path}"}
    try:
        import xmlschema
    except ImportError:
        return {"status": "skipped", "reason": "xmlschema library not installed"}
    try:
        schema = xmlschema.XMLSchema(str(path))
        errors = [str(e.reason or e) for e in schema.iter_errors(xml_text)]
    except Exception as exc:  # noqa: BLE001 — a broken schema is a gate error, not a crash
        return {"status": "error", "reason": str(exc)}
    if errors:
        return {"status": "failed", "errors": errors[:20]}
    return {"status": "passed"}
