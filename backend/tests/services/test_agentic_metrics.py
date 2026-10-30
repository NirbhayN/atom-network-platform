# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Pure aggregation tests for the Phase-B outcome metrics (BACKLOG E1)."""
from app.services.agentic_metrics import summarize_runs


def _ev(kind, **payload):
    return {"kind": kind, "payload": payload}


def test_summarize_runs_core_numbers():
    runs = [
        {"id": "r1", "status": "completed", "kind": "code"},
        {"id": "r2", "status": "completed", "kind": "code"},
        {"id": "r3", "status": "failed", "kind": "full"},
        {"id": "r4", "status": "active", "kind": "code"},      # non-terminal: excluded from rate
    ]
    events = {
        # green on the second verification attempt, after one review round
        "r1": [_ev("review"), _ev("verification", status="needs_fix"),
               _ev("verification", status="verified"), _ev("contract_gate")],
        # NEVER BUILT but completed — the C4 red flag
        "r2": [_ev("verification", status="unverified"), _ev("verify_unverified")],
        "r3": [_ev("review"), _ev("review"), _ev("loop_capped")],
        "r4": [],
    }
    out = summarize_runs(runs, events)
    assert out["runs_total"] == 4
    assert out["by_status"] == {"completed": 2, "failed": 1, "active": 1}
    assert out["success_rate"] == round(2 / 3, 3)              # terminal runs only
    assert out["verify_final"] == {"verified": 1, "needs_fix": 0, "unverified": 1, "none": 2}
    assert out["unverified_completions"] == 1
    assert out["avg_review_rounds"] == 1.5                     # r1=1, r3=2
    assert out["avg_verify_attempts"] == 1.5                   # r1=2, r2=1
    assert out["gates_fired"]["contract_gate"] == 1
    assert out["loop_health"]["loop_capped"] == 1
    assert out["loop_health"]["verify_unverified"] == 1


def test_summarize_runs_empty_is_safe():
    out = summarize_runs([], {})
    assert out["runs_total"] == 0 and out["success_rate"] is None
    assert out["avg_review_rounds"] is None and out["unverified_completions"] == 0
