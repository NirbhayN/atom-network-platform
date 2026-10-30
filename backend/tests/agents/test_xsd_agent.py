# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""XSD generator message assembly (B1): the model must SEE the base schemas it is
told to preserve, on the FIRST turn and on every refine/repair turn (the persisted
conversation strips turn-1's ephemeral context)."""
import asyncio

import pytest

from app.agents import xsd as X


@pytest.fixture
def captured(monkeypatch):
    cap = {}

    async def fake_stream(**kwargs):
        cap.update(kwargs)
        yield "chunk"
    monkeypatch.setattr(X, "stream_llm", fake_stream)
    return cap


async def _drain(gen):
    return "".join([c async for c in gen])


def test_first_turn_carries_spec_brd_and_base_schemas(captured):
    asyncio.run(_drain(X.stream_xsd_turn(
        "THE SPEC", "THE BRD", [], "Generate the XSD changes.",
        base_schemas="<xs:schema>BASE CONTENT</xs:schema>")))
    msgs = captured["messages"]
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    body = msgs[0]["content"]
    assert "THE SPEC" in body and "THE BRD" in body
    assert "CURRENT SCHEMAS" in body and "BASE CONTENT" in body
    assert "PRESERVE existing elements" in body
    assert "Generate the XSD changes." in body


def test_refine_turn_is_reanchored_with_context(captured):
    history = [{"role": "user", "content": "Generate the XSD changes."},
               {"role": "assistant", "content": "### `a.xsd` ..."}]
    asyncio.run(_drain(X.stream_xsd_turn(
        "THE SPEC", "THE BRD", history, "Rename the field.",
        base_schemas="<xs:schema>BASE</xs:schema>")))
    msgs = captured["messages"]
    # context exchange + history + new message, with role alternation intact
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert "THE SPEC" in msgs[0]["content"] and "CURRENT SCHEMAS" in msgs[0]["content"]
    assert msgs[2]["content"] == "Generate the XSD changes."   # history untouched
    assert "Rename the field." in msgs[-1]["content"]


def test_no_base_schemas_keeps_legacy_shape(captured):
    asyncio.run(_drain(X.stream_xsd_turn("SPEC", "BRD", [], "go")))
    body = captured["messages"][0]["content"]
    assert "CURRENT SCHEMAS" not in body


def test_assessment_sees_base_schemas(captured):
    asyncio.run(_drain(X.stream_xsd_assessment(
        "SPEC", "BRD", base_schemas="<xs:schema>EXISTING</xs:schema>")))
    body = captured["messages"][0]["content"]
    assert "CURRENT SCHEMAS" in body and "EXISTING" in body


def test_xsd_names_in_scrapes_file_tokens():
    from app.services.xsd_context import xsd_names_in
    names = xsd_names_in("touches ReqTransfer.xsd and `common_types.xsd`", "also b.xsd")
    assert names == {"ReqTransfer.xsd", "common_types.xsd", "b.xsd"}
    assert xsd_names_in("", None) == set()


# ── B3: section-aware clipping ──────────────────────────────────────────────────────

def test_clip_markdown_keeps_whole_sections_and_names_omissions():
    doc = ("# Intro\nshort intro\n\n## Fields\n" + "f" * 300
           + "\n\n## Regulatory\n" + "r" * 300 + "\n\n## Error Codes\n" + "e" * 300)
    out = X._clip_markdown(doc, 400, "tech spec")
    assert "short intro" in out and "fff" in out
    assert "rrr" not in out and "eee" not in out          # whole sections dropped, not sliced
    assert "clipped to fit" in out and "Regulatory" in out and "Error Codes" in out


def test_clip_markdown_passthrough_and_headingless_fallback():
    assert X._clip_markdown("tiny", 100, "x") == "tiny"
    out = X._clip_markdown("n" * 500, 100, "BRD")          # no headings → hard clip + note
    assert out.startswith("n" * 100) and "clipped to fit" in out


# ── B4: structured REQUIRED/NOT-REQUIRED decision ───────────────────────────────────

def test_decision_reads_the_decision_section_not_the_prose():
    doc = ("## XSD Change Assessment\n\n### Decision\n**NOT REQUIRED**\n\n### Rationale\n"
           "The spec adds a REQUIRED field, but it maps onto the existing schema.\n")
    assert X.parse_xsd_decision(doc) is False              # prose "REQUIRED" must not flip it
    assert X.parse_xsd_decision(doc.replace("**NOT REQUIRED**", "**REQUIRED**")) is True


def test_decision_falls_back_to_legacy_heuristic_without_a_section():
    assert X.parse_xsd_decision("free text saying **REQUIRED** changes") is True
    assert X.parse_xsd_decision("NOT REQUIRED here, despite **REQUIRED** later") is False
    assert X.parse_xsd_decision("") is False
