# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""_safe_title must always produce an openpyxl-valid sheet title."""

import pytest

from app.excel_testcase_engine.excel_writer.renderer import _safe_title


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pay/Collect Scenarios", "Pay-Collect Scenarios"),
        ("Debit\\Credit", "Debit-Credit"),
        ("What? Why*", "What- Why-"),
        ("Scope: Phase 1", "Scope- Phase 1"),
        ("[UAT] Mobile", "-UAT- Mobile"),
        ("'quoted'", "quoted"),
        ("Plain Name", "Plain Name"),
    ],
)
def test_invalid_characters_replaced(raw: str, expected: str) -> None:
    assert _safe_title(raw) == expected


def test_truncates_to_31_chars() -> None:
    assert len(_safe_title("x" * 50)) == 31


def test_empty_after_sanitize_falls_back() -> None:
    assert _safe_title("///") == "---"
    assert _safe_title("''") == "Sheet"
    assert _safe_title("") == "Sheet"


def test_openpyxl_accepts_sanitized_titles() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    for raw in ["Pay/Collect", "a:b", "c[d]e", "?" * 40, "'tick'", ""]:
        wb.create_sheet(_safe_title(raw))  # must not raise
