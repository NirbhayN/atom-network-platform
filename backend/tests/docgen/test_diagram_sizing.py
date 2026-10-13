# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Diagram legibility — wrap wide PlantUML at the source, fit images both ways.

The dangerous mutant is the ILLEGIBLE STRIP: an LLM sequence diagram with long
side-notes renders ~6:1 wide, and a blind `width=Inches(6.5)` embed collapses
it to a ~1-inch band (QA: "there is no diagram in the whole document" — there
was, it was unreadable). These tests pin the two halves of the fix:
  * _prepare_plantuml_source injects wrapWidth/maxMessageSize so the PNG comes
    out page-shaped — without clobbering a source that set its own.
  * _diagram_display_width caps the implied height (tall diagrams), never
    blows up tiny PNGs past 2x native, and falls back to full width on
    unreadable files (the old behaviour).
"""
from __future__ import annotations

from docx.shared import Inches
from PIL import Image

from app.docgen.tools.diagram_generator import _prepare_plantuml_source
from app.docgen.tools.docx_builder import _diagram_display_width


# ── PlantUML source preparation ──────────────────────────────────────────────

def test_wrap_skinparams_injected_after_startuml():
    out = _prepare_plantuml_source("@startuml\nA -> B: hello\n@enduml")
    assert "skinparam wrapWidth 250" in out
    assert "skinparam maxMessageSize 250" in out
    assert "!pragma layout smetana" in out
    # Everything injected before the diagram body, right after @startuml.
    assert out.index("@startuml") < out.index("wrapWidth") < out.index("A -> B")


def test_existing_skinparams_not_clobbered():
    src = "@startuml\nskinparam wrapWidth 999\nskinparam maxMessageSize 111\nA -> B: x\n@enduml"
    out = _prepare_plantuml_source(src)
    assert out.count("wrapWidth") == 1 and "wrapWidth 999" in out
    assert out.count("maxMessageSize") == 1 and "maxMessageSize 111" in out


def test_partial_existing_skinparam_only_missing_one_added():
    src = "@startuml\nskinparam wrapWidth 300\nA -> B: x\n@enduml"
    out = _prepare_plantuml_source(src)
    assert out.count("wrapWidth") == 1
    assert "maxMessageSize 250" in out


# ── DOCX display sizing ──────────────────────────────────────────────────────

def _png(tmp_path, w, h):
    p = tmp_path / f"img_{w}x{h}.png"
    Image.new("RGB", (w, h), "white").save(p)
    return str(p)


def test_normal_diagram_gets_full_page_width(tmp_path):
    # 1600x900 (16:9) — classic flowchart shape, full 6.5" is right.
    assert _diagram_display_width(_png(tmp_path, 1600, 900)) == Inches(6.5)


def test_tall_diagram_width_reduced_to_fit_page_height(tmp_path):
    # 500x2000 (1:4): at 6.5" wide it would be 26" tall. Height cap 7.5"
    # implies width 1.875", floored to the 2" readability minimum.
    assert _diagram_display_width(_png(tmp_path, 500, 2000)) == Inches(2.0)


def test_tiny_png_not_upscaled_past_2x_native(tmp_path):
    # 200x100 is ~2.08" native at 96dpi — 2x cap gives ~4.17", not 6.5".
    w = _diagram_display_width(_png(tmp_path, 200, 100))
    assert w < Inches(6.5)
    assert abs(w - Inches(2 * 200 / 96)) < Inches(0.05)


def test_unreadable_image_falls_back_to_full_width(tmp_path):
    bad = tmp_path / "not_an_image.png"
    bad.write_text("nope")
    assert _diagram_display_width(str(bad)) == Inches(6.5)
