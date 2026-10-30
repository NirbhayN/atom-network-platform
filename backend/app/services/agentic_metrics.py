# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Phase-B outcome metrics (BACKLOG E1) — aggregated from the data the runs
already emit.

Accuracy work on the agentic pipeline was unmeasurable: run outcomes lived only
as raw ``agentic_events`` rows and per-LLM-call traces, so "did that change help"
meant reading transcripts. This module turns the existing run rows + event stream
into the handful of numbers that question needs:

* success rate (terminal runs that completed),
* final verification status per run — including the **unverified completions**
  (runs that reached approval NEVER BUILT, C4),
* review pressure (review rounds per run, loop-capped / blocked runs),
* gate activity (how often each advisory gate actually fired — the shadow-mode
  evidence for hardening or deleting a gate).

Split pure/IO on the house pattern: :func:`summarize_runs` is pure dict-in →
dict-out (unit-testable, no DB), :func:`collect_metrics` is the thin query
wrapper the admin endpoint calls."""
from __future__ import annotations

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

# Event kinds that represent an analytical gate FIRING (shadow or enforcing) —
# the measure half of the slipstream posture.
GATE_KINDS = ("contract_gate", "di_wiring_gate", "cross_module_gate",
              "acceptance_predicates", "plan_fidelity", "plan_fidelity_advisory",
              "plan_fidelity_stale", "tsd_approval_gate")
# Loop-health kinds worth counting per run.
LOOP_KINDS = ("review", "review_blocked", "loop_capped", "plan_incomplete",
              "verify_decision_needed", "history_compacted", "verify_unverified")
_WATCH_KINDS = GATE_KINDS + LOOP_KINDS + ("verification",)

_TERMINAL = {"completed", "failed"}


def summarize_runs(runs: list[dict], events_by_run: dict[str, list[dict]]) -> dict:
    """Pure aggregation. ``runs``: [{id, status, kind, phase}], ``events_by_run``:
    {run_id: [{kind, payload}]} (ascending event order)."""
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    verify_final: dict[str, int] = {"verified": 0, "needs_fix": 0, "unverified": 0, "none": 0}
    gates: dict[str, int] = {k: 0 for k in GATE_KINDS}
    loop: dict[str, int] = {k: 0 for k in LOOP_KINDS}
    unverified_completions = 0
    review_round_counts: list[int] = []
    verify_attempt_counts: list[int] = []

    for run in runs:
        rid = run.get("id") or ""
        status = (run.get("status") or "unknown").lower()
        by_status[status] = by_status.get(status, 0) + 1
        k = run.get("kind") or "full"
        by_kind[k] = by_kind.get(k, 0) + 1

        events = events_by_run.get(rid) or []
        last_verify = None
        reviews = 0
        verifies = 0
        for e in events:
            ek = e.get("kind") or ""
            if ek == "verification":
                verifies += 1
                last_verify = ((e.get("payload") or {}).get("status") or "").lower() or last_verify
            elif ek == "review":
                reviews += 1
            if ek in gates:
                gates[ek] += 1
            if ek in loop:
                loop[ek] += 1
        if reviews:
            review_round_counts.append(reviews)
        if verifies:
            verify_attempt_counts.append(verifies)
        verify_final[last_verify if last_verify in verify_final else "none"] += 1
        if status == "completed" and last_verify == "unverified":
            unverified_completions += 1

    terminal = sum(n for s, n in by_status.items() if s in _TERMINAL)
    completed = by_status.get("completed", 0)
    return {
        "runs_total": len(runs),
        "by_status": by_status,
        "by_kind": by_kind,
        # Of the runs that REACHED a terminal state, how many completed. Active/paused
        # runs are excluded — counting them as failures would punish a busy system.
        "success_rate": round(completed / terminal, 3) if terminal else None,
        "verify_final": verify_final,
        # Completed runs that were NEVER BUILT (DeferredVerifier) — each one is a
        # change a human approved without a compile. Should be zero on a healthy host.
        "unverified_completions": unverified_completions,
        "avg_review_rounds": (round(sum(review_round_counts) / len(review_round_counts), 2)
                              if review_round_counts else None),
        # Verification attempts per run ≈ rounds-to-green: 1 is the ideal; a rising
        # average means the code phase ships builds that don't compile first try.
        "avg_verify_attempts": (round(sum(verify_attempt_counts) / len(verify_attempt_counts), 2)
                                if verify_attempt_counts else None),
        "gates_fired": gates,
        "loop_health": loop,
    }


def collect_metrics(db, *, days: int = 30, limit: int = 500) -> dict:
    """Query wrapper: the last ``limit`` runs started in the last ``days`` days,
    with only the watched event kinds pulled (kind + payload, no bulk)."""
    from app.models.agentic import AgenticEvent, AgenticRun
    from app.models.base import utcnow

    since = utcnow() - timedelta(days=max(1, days))
    rows = (db.query(AgenticRun)
            .filter(AgenticRun.created_at >= since)
            .order_by(AgenticRun.created_at.desc())
            .limit(max(1, min(limit, 2000))).all())
    runs = [{"id": r.id, "status": r.status, "kind": r.kind, "phase": r.phase} for r in rows]
    events_by_run: dict[str, list[dict]] = {r["id"]: [] for r in runs}
    if runs:
        ev_rows = (db.query(AgenticEvent.run_id, AgenticEvent.kind, AgenticEvent.payload)
                   .filter(AgenticEvent.run_id.in_(list(events_by_run)),
                           AgenticEvent.kind.in_(_WATCH_KINDS))
                   .order_by(AgenticEvent.run_id, AgenticEvent.seq).all())
        for run_id, kind, payload in ev_rows:
            events_by_run[run_id].append({"kind": kind, "payload": payload or {}})
    out = summarize_runs(runs, events_by_run)
    out["window_days"] = days
    return out
