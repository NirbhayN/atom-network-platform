# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Working habits W1–W7: the mechanisms that make a run stop-safe at any moment,
every claim checkable, and the tree never left broken.

These are deliberately HARNESS-side tests: each mechanism exists because the model
cannot be trusted to self-report the thing being measured (its own spend, whether it
actually ran the build, what it left behind when it stopped).
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents import agentic_runtime as RT
from app.agents.agentic_tools import RunContext
from app.core.config import settings
from app.core.llm import MessagesToolTurn, ToolUseRequest


# ── shared loop harness (same shape as test_fact_sheet.py) ───────────────────────

def _turn(tool_uses, text="", stop="tool_use"):
    ac = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses]
    return MessagesToolTurn(text=text, tool_uses=tool_uses, stop_reason=stop, assistant_content=ac)


def _script(monkeypatch, turns):
    st = {"i": 0}

    async def fake(**kwargs):
        t = turns[min(st["i"], len(turns) - 1)]
        st["i"] += 1
        return t
    monkeypatch.setattr(RT, "call_messages_api_tools", fake)
    return st


def _capture_events(monkeypatch):
    ev = []
    monkeypatch.setattr(RT, "emit_event",
                        lambda db, rid, kind, payload=None: ev.append((kind, payload)))
    return ev


def _loop(**kw):
    return asyncio.run(RT.run_agent_loop(
        run_id="r", selected_repo_ids=["core"], system="s", user_prompt="go",
        tools=[], require_plan=False, db=SimpleNamespace(), **kw))


def _budget(monkeypatch, *, weighted: int, cap: int):
    """Pin the ledger reading the budget guard sees, and the enforced cap."""
    import app.core.observability as O
    monkeypatch.setattr(O, "get_run_token_breakdown", lambda _rid: {
        "weighted_total": weighted, "input_tokens": weighted, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0}, raising=False)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", cap)
    monkeypatch.setattr(settings, "agentic_token_budget_enforce", True)


# ── W1: budget visibility ────────────────────────────────────────────────────────

def test_budget_line_is_silent_without_a_reading_or_a_cap():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    assert RT._budget_state_line(ctx) is None, "no reading taken yet"
    ctx.budget_used_pct, ctx.budget_cap = 40, 0
    assert RT._budget_state_line(ctx) is None, "no cap configured → nothing to report"


def test_budget_line_states_the_harness_measured_percentage():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.budget_used_pct, ctx.budget_spent, ctx.budget_cap = 30, 300_000, 1_000_000
    line = RT._budget_state_line(ctx)
    assert "~30% used" in line and "300,000 of 1,000,000" in line
    assert "harness-measured" in line
    assert "DURABLE" in line, "under the warn line → sequencing advice"
    ctx.budget_used_pct, ctx.budget_spent = 85, 850_000
    late = RT._budget_state_line(ctx)
    assert "~85% used" in late and "STOP opening new work" in late


def test_budget_line_rides_the_pinned_ground_truth_block():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    ctx.budget_used_pct, ctx.budget_spent, ctx.budget_cap = 50, 5, 10
    messages = [{"role": "user", "content": "the brief"}]
    RT._pin_ground_truth(messages, ctx)
    assert RT._STATE_MARKER in messages[0]["content"]
    assert "~50% used" in messages[0]["content"]


def test_loop_pins_the_budget_reading_and_only_on_bucket_change(monkeypatch):
    # The loop takes the reading from the SAME ledger query the hard-stop uses, so the
    # number the model sees can never disagree with the number that stops it.
    _capture_events(monkeypatch)
    _budget(monkeypatch, weighted=640_000, cap=1_000_000)
    _script(monkeypatch, [_turn([ToolUseRequest("a", "glob", {"pattern": "*.java"})]),
                          _turn([], text="done", stop="end_turn")])
    res = _loop()
    brief = res.transcript[0]["content"]
    assert "~64% used" in brief and "640,000 of 1,000,000" in brief
    assert brief.count(RT._STATE_MARKER) == 1, "re-pin replaces, never accumulates"


def test_loop_does_not_repin_within_the_same_bucket(monkeypatch):
    # Every re-pin rewrites messages[0] and costs a prompt-cache re-read. A flat spend
    # must not pay that on every turn.
    _capture_events(monkeypatch)
    _budget(monkeypatch, weighted=120_000, cap=1_000_000)
    pins = {"n": 0}
    real = RT._pin_ground_truth
    monkeypatch.setattr(RT, "_pin_ground_truth",
                        lambda m, c: (pins.__setitem__("n", pins["n"] + 1), real(m, c))[1])
    _script(monkeypatch, [_turn([ToolUseRequest("a", "glob", {"pattern": "*.java"})])] * 4
            + [_turn([], text="done", stop="end_turn")])
    _loop(max_iterations=5)
    assert pins["n"] == 1, f"one pin for the 10%-bucket, got {pins['n']}"


def test_budget_reading_is_taken_even_on_the_turn_the_cap_trips(monkeypatch):
    # The stop is a soft stop between turns: the brief that lands in the persisted
    # transcript must already say the run was out of budget, not look healthy.
    _capture_events(monkeypatch)
    _budget(monkeypatch, weighted=1_000_000, cap=1_000_000)
    _script(monkeypatch, [_turn([], text="done", stop="end_turn")])
    res = _loop()
    assert res.stopped == "budget_exceeded"
    assert "~100% used" in res.transcript[0]["content"]


def test_budget_visibility_is_inert_when_no_cap_is_configured(monkeypatch):
    _capture_events(monkeypatch)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)
    _script(monkeypatch, [_turn([], text="done", stop="end_turn")])
    res = _loop()
    assert "Run token budget" not in res.transcript[0]["content"]
