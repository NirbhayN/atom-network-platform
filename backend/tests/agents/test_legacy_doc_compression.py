# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""A6 — the legacy compressor must never drop constraint sections again, and must
NAME what it does drop (a bare count reads as 'nothing important')."""
from app.agents.code_change import _compress_doc_for_code_change


def _doc():
    return ("Title line\n\n"
            "## Data Model\n" + "d" * 200 + "\n\n"
            "## Regulatory Requirements\n" + "MANDATE-7 applies. " * 10 + "\n\n"
            "## Compliance Checklist\n" + "c" * 150 + "\n\n"
            "## Executive Summary\n" + "fluff " * 100 + "\n\n"
            "## Glossary\n" + "g" * 400)


def test_regulatory_and_compliance_sections_survive_compression():
    out = _compress_doc_for_code_change(_doc(), "BRD", budget=900)
    assert "MANDATE-7" in out, "regulatory section was dropped — the exact legacy bug"
    assert "## Compliance Checklist" in out
    assert "Executive Summary" not in out.split("compressed for prompt budget")[0] \
        or "fluff" not in out, "narrative fluff should not displace constraints"


def test_dropped_sections_are_named_in_the_marker():
    out = _compress_doc_for_code_change(_doc(), "BRD", budget=900)
    assert "compressed for prompt budget" in out
    assert "Executive Summary" in out or "Glossary" in out, \
        "the marker must NAME dropped sections, not just count them"
