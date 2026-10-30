# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Deterministic XML-block validation for generated docs (accuracy S6).

A TSD / Product-Kit legitimately contains two kinds of ```xml block:
  - SCHEMA blocks (root ``<xs:schema>`` / ``<schema ...XMLSchema>``) — the doc must
    REPRODUCE an approved schema, never re-author it. Checked by whitespace-normalised
    substring match against the approved schema files.
  - INSTANCE blocks (sample request/response messages) — must VALIDATE against the
    approved XSD set (so the doc doesn't invent elements). Checked via ``xmlschema``.

The earlier plan's "every XML block is an excerpt of a schema" predicate was wrong:
instance documents validate AGAINST a schema, they are never substrings OF it. This
splits the two correctly.

Advisory + FAIL-OPEN: any parse/schema-build/namespace-resolution error yields NO
finding (we never block a doc because the validator couldn't construct the schema).
Returns a list of human-readable finding strings (empty == clean).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_XML_BLOCK = re.compile(r"```xml\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_WS = re.compile(r"\s+")


def extract_xml_blocks(markdown: str) -> list[str]:
    """Every ```xml fenced block body in the markdown."""
    if not markdown:
        return []
    return [m.group(1).strip() for m in _XML_BLOCK.finditer(markdown) if m.group(1).strip()]


def _is_schema_block(xml: str) -> bool:
    head = xml.lstrip()[:400].lower()
    return ("xs:schema" in head or "xsd:schema" in head
            or ("<schema" in head and "xmlschema" in head))


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def validate_xml_blocks(markdown: str, approved_schemas: list[str]) -> list[str]:
    """Validate the doc's XML blocks against the approved schemas. '' approved set
    (no XSDs yet) → no findings (nothing to check against)."""
    blocks = extract_xml_blocks(markdown)
    if not blocks or not approved_schemas:
        return []

    findings: list[str] = []
    norm_schemas = [_norm(s) for s in approved_schemas if s and s.strip()]

    # Build instance validators once (fail-open: skip any schema that won't build).
    built = []
    try:
        import xmlschema
        for s in approved_schemas:
            if not (s and s.strip()):
                continue
            try:
                built.append(xmlschema.XMLSchema(s))
            except Exception as e:  # noqa: BLE001 — unbuildable (imports/namespace) → skip
                logger.debug("xsd_validation: schema did not build (%s) — skipping", e)
    except Exception as e:  # noqa: BLE001 — xmlschema unavailable
        logger.debug("xsd_validation: xmlschema unavailable (%s)", e)

    for i, block in enumerate(blocks):
        if _is_schema_block(block):
            # Must reproduce an approved schema region (whitespace-normalised).
            nb = _norm(block)
            if nb and not any(nb in ns for ns in norm_schemas):
                findings.append(
                    f"XML block #{i + 1} is a SCHEMA that does not match any approved XSD "
                    "(reproduce the approved schema verbatim; do not author new schema)."
                )
        elif built:
            # Instance message — valid if it validates against ANY approved schema.
            ok = False
            well_formed = True
            for sch in built:
                try:
                    if sch.is_valid(block):
                        ok = True
                        break
                except Exception:  # noqa: BLE001 — not parseable against this schema
                    continue
            if not ok:
                # Only flag a well-formed instance that matched no schema (fail-open on
                # parse errors so a malformed snippet isn't double-reported here).
                try:
                    import xml.etree.ElementTree as ET
                    ET.fromstring(block)
                except Exception:  # noqa: BLE001
                    well_formed = False
                if well_formed:
                    findings.append(
                        f"XML block #{i + 1} is a sample message that does not validate against "
                        "any approved XSD (it may reference elements not in the decided schema)."
                    )
    return findings


# ── Compile gate for GENERATED schema markdown (BACKLOG B2) ───────────────────────────
# The doc-path XSD generator emits whole schema files as ```xml blocks; nothing ever
# compiled them, so truncated XML, broken facets and malformed structure reached
# partners unchecked. This gate is DETERMINISTIC and feeds the generation repair loop:
# every block must be well-formed XML, rooted at xs:schema, and build as an XML Schema.
# Blocks are materialized into one sandbox dir under their declared filenames first,
# so include/import BETWEEN the document's own files resolves locally — references to
# schemas outside the document (the ecosystem's base set) are NOT defects here, and
# the unknown-component fallout they cause is filtered out with them.

_XSD_NS = "http://www.w3.org/2001/XMLSchema"
# Error-message classes caused by a reference LEAVING the sandbox (not intrinsic defects).
_RESOLUTION_ERR = ("import", "include", "schemalocation", "cannot load", "unable to load",
                   "access not allowed", "no such file", "not found", "redefine", "url")
_UNKNOWN_REF_ERR = ("unknown", "unresolved", "missing", "reference")
_COMPILE_ERR_CAP = 12


def _first_line(err) -> str:
    return str(err).strip().splitlines()[0][:300]


def compile_generated_schemas(markdown: str) -> list[str]:
    """Compile every generated ```xml schema block. Returns human-readable error
    strings (empty == clean). Well-formedness is checked with the stdlib (always
    available); the full XML Schema build needs ``xmlschema`` and degrades to the
    well-formedness tier when absent. Never raises (a checker fault must not kill
    generation) — but a genuinely broken block is always reported."""
    try:
        from app.services.xsd_bundle import extract_xsd_blocks
        blocks = extract_xsd_blocks(markdown)
    except Exception:  # noqa: BLE001 — extraction fault → nothing to check
        logger.warning("compile_generated_schemas: block extraction failed", exc_info=True)
        return []
    if not blocks:
        return []

    errors: list[str] = []
    well_formed: dict[str, str] = {}      # sanitized filename -> xml (last block wins)
    ext_refs: dict[str, set[str]] = {}    # filename -> import/include locations it declares
    import os
    import xml.etree.ElementTree as ET
    for fname, xml in blocks:
        safe = os.path.basename(fname or "").strip() or "schema.xsd"
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            errors.append(f"{safe}: not well-formed XML — {e}")
            continue
        if root.tag != f"{{{_XSD_NS}}}schema":
            errors.append(f"{safe}: root element is {root.tag!r}, not an xs:schema — every "
                          "```xml block in this document must be a complete schema file")
            continue
        well_formed[safe] = xml
        ext_refs[safe] = {os.path.basename(el.get("schemaLocation") or "")
                          for tag in ("import", "include", "redefine")
                          for el in root.findall(f"{{{_XSD_NS}}}{tag}")
                          if el.get("schemaLocation")}

    if not well_formed or errors and not well_formed:
        return errors[:_COMPILE_ERR_CAP]

    try:
        import xmlschema
    except Exception:  # noqa: BLE001 — optional dep absent → well-formedness tier only
        logger.info("compile_generated_schemas: xmlschema unavailable — well-formedness only")
        return errors[:_COMPILE_ERR_CAP]

    import tempfile
    from pathlib import Path as _P
    try:
        with tempfile.TemporaryDirectory(prefix="xsdgen_") as tmp:
            for safe, xml in well_formed.items():
                _P(tmp, safe).write_text(xml, encoding="utf-8")
            for safe in well_formed:
                try:
                    # allow='sandbox': resolution confined to the temp dir — sibling
                    # blocks resolve by filename, nothing escapes to disk or network.
                    schema = xmlschema.XMLSchema(str(_P(tmp, safe)), validation="lax",
                                                 allow="sandbox")
                    block_errs = [_first_line(e) for e in (getattr(schema, "all_errors", None) or [])]
                except Exception as e:  # noqa: BLE001 — some defects raise even on lax
                    block_errs = [_first_line(e)]
                lower = [e.lower() for e in block_errs]
                # The document references a schema OUTSIDE itself when (a) a declared
                # import/include location is not one of the document's own files —
                # deterministic, and necessary because xmlschema reports a failed
                # import as a WARNING, not an error — or (b) an error message is
                # resolution-shaped. Either way, unknown-reference fallout is the
                # missing external's fault, not this document's defect.
                left_sandbox = (any(loc not in well_formed for loc in ext_refs.get(safe, ()))
                                or any(any(k in e for k in _RESOLUTION_ERR) for e in lower))
                for msg, low in zip(block_errs, lower):
                    if any(k in low for k in _RESOLUTION_ERR):
                        continue           # external base schema — not this document's defect
                    if left_sandbox and any(k in low for k in _UNKNOWN_REF_ERR):
                        continue           # fallout of the unresolved external, not intrinsic
                    errors.append(f"{safe}: {msg}")
                    if len(errors) >= _COMPILE_ERR_CAP:
                        return errors[:_COMPILE_ERR_CAP]
    except Exception:  # noqa: BLE001 — checker fault must never kill generation
        logger.warning("compile_generated_schemas: compile tier failed", exc_info=True)
    return errors[:_COMPILE_ERR_CAP]
