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


# ── W2: ran-before-done ──────────────────────────────────────────────────────────

def test_executions_counts_only_what_actually_ran():
    ctx = RunContext(run_id="r", selected_repo_ids=["core"])
    assert ctx.executions == 0
    ctx.command_runs, ctx.verify_runs = 2, 1
    assert ctx.executions == 3, "one number every consumer reads"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A RunContext over a real one-repo workspace clone (git-initialised, so the
    allowlisted git commands actually run)."""
    import subprocess

    import app.agents.agentic_tools as T
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-1" / "repo-1"
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    return T, T.RunContext(run_id="run-1", selected_repo_ids=["repo-1"])


def test_run_command_counts_the_run_but_a_refused_command_does_not(ws):
    T, ctx = ws
    out = T.run_command(ctx, repo_id="repo-1", argv=["git", "status", "--porcelain"])
    assert "exit=" in out and ctx.command_runs == 1
    with pytest.raises(T.ToolError):
        T.run_command(ctx, repo_id="repo-1", argv=["curl", "http://example.invalid"])
    assert ctx.command_runs == 1, "a command the allowlist refused never ran"


def test_verify_change_counts_the_build_but_not_the_nothing_to_verify_path(ws, monkeypatch):
    T, ctx = ws
    assert "no changes yet to verify" in T.verify_change(ctx, repo_id="repo-1")
    assert ctx.verify_runs == 0, "nothing was edited, so nothing was submitted to a build"
    monkeypatch.setattr(T, "_disk_ops", lambda _c: (
        [T.FileOp(op="modify", repo_id="repo-1", path="src/A.java",
                  content="x", content_hash="h")], []))
    monkeypatch.setattr("app.agents.verifier.select_verifier", lambda: SimpleNamespace(
        name="local", verify=lambda *a, **k: SimpleNamespace(
            status="needs_fix", gates={"compile": False}, reason="", module_results={},
            rounds=[])))
    T.verify_change(ctx, repo_id="repo-1")
    assert ctx.verify_runs == 1, "counted on needs_fix too — it RAN, it just failed"


def _edit_and_declare_done(monkeypatch, *, run_first=False):
    """Script: edit a file (optionally run something), then declare done."""
    from app.agents.agentic_tools import FileOp

    def _execute(ctx, name, inp):
        if name == "edit_file":
            p = inp.get("path") or "src/A.java"
            ctx.file_ops[("core", p)] = FileOp("modify", "core", p, "x", "h")
            return "edited " + p, False
        if name == "run_command":
            ctx.command_runs += 1
            return "exit=0", False
        return "ok", False
    monkeypatch.setattr(RT.T, "execute_tool", _execute)
    monkeypatch.setattr(RT.T, "intel_gate_reason", lambda *a, **k: None)
    first = [ToolUseRequest("e", "edit_file", {"repo_id": "core", "path": "src/A.java"})]
    if run_first:
        first.append(ToolUseRequest("c", "run_command", {"repo_id": "core", "argv": ["mvn", "-q", "compile"]}))
    return _script(monkeypatch, [_turn(first), _turn([], text="all done", stop="end_turn")])


def test_edits_with_zero_runs_are_nudged_for_an_observed_result(monkeypatch):
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)
    _edit_and_declare_done(monkeypatch)
    res = _loop(code_phase=True, max_iterations=4)
    nudges = [p for k, p in events if k == "ran_nothing_nudge"]
    assert nudges and nudges[0]["edits"] == 1
    injected = [m for m in res.transcript
                if m.get("role") == "user" and isinstance(m.get("content"), str)
                and "ZERO times" in m["content"]]
    assert injected, "the nudge is a real user turn the model must answer"
    assert "QUOTE the output you actually saw" in injected[0]["content"]
    assert res.executions == 0, "carried out of the loop so an ignored nudge stays visible"


def test_the_ran_before_done_nudge_fires_at_most_once(monkeypatch):
    # The model is allowed to disagree. Spending one turn on the ask is the whole
    # budget — a second would be a loop against an agent that already answered.
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)
    _edit_and_declare_done(monkeypatch)
    res = _loop(code_phase=True, max_iterations=6)
    assert len([1 for k, _ in events if k == "ran_nothing_nudge"]) == 1
    assert res.stopped == "completed", "the second 'done' is accepted"


def test_no_nudge_when_the_agent_actually_ran_something(monkeypatch):
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)
    _edit_and_declare_done(monkeypatch, run_first=True)
    res = _loop(code_phase=True, max_iterations=4)
    assert not any(k == "ran_nothing_nudge" for k, _ in events)
    assert res.executions == 1


def test_read_only_phases_are_never_nudged(monkeypatch):
    # Analysis / review loops have nothing to build; a "run the build" nudge there is
    # a wasted turn against an agent with no build to run.
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)
    _edit_and_declare_done(monkeypatch)
    _loop(code_phase=False, max_iterations=4)
    assert not any(k == "ran_nothing_nudge" for k, _ in events)


def _phase_code_with(monkeypatch, *, executions, disk_ops, stopped="completed"):
    """Drive _phase_code over a stubbed code round; returns the emitted events."""
    from app.agents import agentic_orchestrator as O
    from app.agents.agentic_subagents import ChangeSet

    events = []
    monkeypatch.setattr(O, "emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O, "_disk_change_count", lambda _run: disk_ops)
    monkeypatch.setattr(settings, "use_self_correction", False)

    async def fake(db, **kw):
        return ChangeSet(operations=[SimpleNamespace(path="f.java")] * disk_ops,
                         stopped=stopped, iterations=10, executions=executions)
    monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)
    run = SimpleNamespace(id="run-x", change_request_id="cr-x", selected_repo_ids=[],
                          attempts_json={}, handoff_json=None, parent_run_id=None)
    art = {"ctx": None, "intent": "x"}
    asyncio.run(O._phase_code(None, run, art, None))
    return events, art


def test_phase_code_reports_a_round_that_edited_but_never_built(monkeypatch):
    # The in-loop nudge is advisory, and a cap can end a round before the agent
    # complies — so the phase CAN finish with edits and zero runs. Say so, rather than
    # letting the verify verdict a minute later look like a surprise.
    events, art = _phase_code_with(monkeypatch, executions=0, disk_ops=3)
    assert art["executions"] == 0
    ran = [p for k, p in events if k == "ran_nothing"]
    assert ran and ran[0]["ops"] == 3
    assert "FIRST execution" in ran[0]["action"]
    assert [p for k, p in events if k == "change_set"][0]["executions"] == 0


def test_phase_code_stays_quiet_when_the_author_built_its_own_work(monkeypatch):
    events, art = _phase_code_with(monkeypatch, executions=4, disk_ops=3)
    assert art["executions"] == 4
    assert not any(k == "ran_nothing" for k, _ in events)


def test_phase_code_stays_quiet_when_there_was_nothing_to_build(monkeypatch):
    events, _ = _phase_code_with(monkeypatch, executions=0, disk_ops=0)
    assert not any(k == "ran_nothing" for k, _ in events)


def test_no_nudge_when_nothing_was_edited(monkeypatch):
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)
    _script(monkeypatch, [_turn([], text="no change needed", stop="end_turn")])
    _loop(code_phase=True)
    assert not any(k == "ran_nothing_nudge" for k, _ in events), \
        "'no change is needed' is a legitimate finished state"
