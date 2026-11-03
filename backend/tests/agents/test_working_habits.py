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


# ── W3: last-green pointer ───────────────────────────────────────────────────────

def test_latest_round_ckpt_sorts_numerically_not_lexically():
    from app.agents.agentic_orchestrator import _latest_round_ckpt
    assert _latest_round_ckpt(None) is None
    assert _latest_round_ckpt({}) is None
    assert _latest_round_ckpt({"round_ckpts": {}}) is None
    h = {"round_ckpts": {"r1": {}, "r2": {}, "r10": {}, "r9": {}}}
    assert _latest_round_ckpt(h) == "r10", "'r10' must not rank below 'r2'"


def _verify_run(handoff=None):
    return SimpleNamespace(id="run-x", handoff_json=handoff, selected_repo_ids=[])


def _stamp(monkeypatch, handoff, *, files=7, attempt=2):
    from app.agents import agentic_orchestrator as O
    events = []
    monkeypatch.setattr(O, "emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O, "_disk_change_count", lambda _run: files)
    run, art = _verify_run(handoff), {}
    O._stamp_last_green(None, run, art, attempt)
    return run, art, events


def test_green_verify_pins_the_round_whose_work_is_in_the_green_tree(monkeypatch):
    run, art, events = _stamp(monkeypatch, {"round_ckpts": {"r1": {}, "r2": {}, "r3": {}}})
    rec = run.handoff_json["last_green_ckpt"]
    assert rec["label"] == "r3" and rec["files"] == 7 and rec["verify_attempt"] == 2
    assert rec["at"], "timestamped — 'last' is only meaningful against a clock"
    # The note must be explicit: reverting TO r3 would undo the round that made it green.
    assert "AFTER the round" in rec["note"] and "BEFORE that later round" in rec["note"]
    assert art["last_green_ckpt"] == rec, "in-process consumers (stop report) see it too"
    green = [p for k, p in events if k == "last_green"]
    assert green and "after round r3" in green[0]["action"]


def test_green_without_any_checkpoint_records_the_fact_and_says_it_is_not_rewindable(monkeypatch):
    # Phase A never rounds, and checkpointing is fail-open — so a green build with no
    # label is normal. Record the green, and never invent a rewind target for it.
    run, _, events = _stamp(monkeypatch, {}, files=2)
    rec = run.handoff_json["last_green_ckpt"]
    assert rec["label"] is None and rec["files"] == 2
    assert "not rewindable by label" in rec["note"]
    assert [p for k, p in events if k == "last_green"][0]["action"] == \
        "🟢 Last green: 2 file(s) building"


def test_stamping_last_green_preserves_the_rest_of_the_handoff(monkeypatch):
    run, _, _ = _stamp(monkeypatch, {"round_ckpts": {"r1": {}}, "approach_decision": {"a": 1},
                                     "xsd_files": [{"path": "x.xsd"}]})
    assert run.handoff_json["approach_decision"] == {"a": 1}
    assert run.handoff_json["xsd_files"] == [{"path": "x.xsd"}]
    assert run.handoff_json["last_green_ckpt"]["label"] == "r1"


def test_a_stamp_failure_never_fails_a_green_build(monkeypatch):
    from app.agents import agentic_orchestrator as O
    monkeypatch.setattr(O, "_disk_change_count", lambda _run: 1 / 0)
    O._stamp_last_green(None, _verify_run({}), {}, 1)      # must not raise


def test_phase_verify_stamps_only_on_green(monkeypatch):
    from app.agents import agentic_orchestrator as O
    seen = []
    monkeypatch.setattr(O, "emit_event", lambda db, rid, kind, payload=None: None)
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=[]))
    monkeypatch.setattr(O, "_stamp_last_green",
                        lambda db, run, art, attempt: seen.append(attempt))
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws")
    for status in ("needs_fix", "unverified", "verified"):
        monkeypatch.setattr(O, "select_verifier", lambda s=status: SimpleNamespace(
            name="local", verify=lambda *a, **k: SimpleNamespace(
                status=s, gates={}, reason="r", module_results={}, rounds=[])))
        O._phase_verify(None, _verify_run({}), {})
    assert seen == [1], "one stamp, for the green verdict only"


def test_run_view_surfaces_the_last_green_pointer(monkeypatch):
    import app.api.agentic as A
    from app.api.agentic import _run_view
    monkeypatch.setattr(A, "_push_view", lambda run: {})   # needs a mapped instance
    rec = {"label": "r2", "at": "2026-11-03T00:00:00", "files": 4}
    run = SimpleNamespace(
        id="r", change_request_id="c", phase="verify", status="running",
        manifest_hash=None, cancel_requested=False, error=None, error_code=None,
        created_by=None, kind="code", parent_run_id=None, selected_repo_ids=[],
        handoff_json={"last_green_ckpt": rec})
    assert _run_view(run)["last_green_ckpt"] == rec
    run.handoff_json = {}
    assert _run_view(run)["last_green_ckpt"] is None, \
        "no green recorded → no known-good state to fall back to"


# ── W4: lessons from FAILED runs ─────────────────────────────────────────────────

def test_failed_run_material_leads_with_how_it_died():
    from app.services.agentic_lessons import _material

    class _DB:
        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def all(self):
            return []

    run = SimpleNamespace(id="r", status="failed", error_code="JDK_MISMATCH",
                          error="module igw-core needs JDK 8 but the reactor ran 17",
                          handoff_json={})
    mat = _material(_DB(), run)
    assert "THIS RUN FAILED" in mat and "code JDK_MISMATCH" in mat
    assert "needs JDK 8" in mat
    assert "property of the repos" in mat, "told to generalise only what recurs"
    # a completed run says nothing about dying
    assert "THIS RUN FAILED" not in _material(
        _DB(), SimpleNamespace(id="r", status="completed", error=None, error_code=None,
                               handoff_json={}))


def _lessons_spy(monkeypatch, *, added=2, boom=False):
    from app.agents import agentic_orchestrator as O
    from app.services import agentic_lessons as AL
    calls, events = [], []

    async def fake(run_id):
        calls.append(run_id)
        if boom:
            raise RuntimeError("distillation exploded")
        return added
    monkeypatch.setattr(AL, "distill_for_run", fake)
    monkeypatch.setattr(O, "emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(settings, "agentic_lessons_enabled", True, raising=False)
    return O, calls, events


def test_a_failed_run_teaches_and_the_event_says_so(monkeypatch):
    O, calls, events = _lessons_spy(monkeypatch)
    asyncio.run(O._distill_lessons(SimpleNamespace(commit=lambda: None), "run-9",
                                   outcome="failed"))
    assert calls == ["run-9"]
    learned = [p for k, p in events if k == "lessons_learned"]
    assert learned and learned[0]["count"] == 2 and learned[0]["outcome"] == "failed"
    assert "FAILED run" in learned[0]["action"], "an operator must see where this came from"


def test_a_completed_run_uses_the_same_path_without_the_failed_wording(monkeypatch):
    O, calls, events = _lessons_spy(monkeypatch)
    asyncio.run(O._distill_lessons(SimpleNamespace(commit=lambda: None), "run-9",
                                   outcome="completed"))
    learned = [p for k, p in events if k == "lessons_learned"][0]
    assert learned["outcome"] == "completed" and "FAILED" not in learned["action"]


def test_distillation_that_learned_nothing_is_silent(monkeypatch):
    O, calls, events = _lessons_spy(monkeypatch, added=0)
    asyncio.run(O._distill_lessons(SimpleNamespace(commit=lambda: None), "r", outcome="failed"))
    assert calls == ["r"] and not events, "0 lessons is not news"


def test_distillation_failure_never_propagates(monkeypatch):
    # Called on a run that has ALREADY failed — raising here would turn a recorded
    # failure into an unhandled one in the worker.
    O, _, events = _lessons_spy(monkeypatch, boom=True)
    asyncio.run(O._distill_lessons(SimpleNamespace(commit=lambda: None), "r", outcome="failed"))
    assert not events


def test_lessons_can_be_switched_off(monkeypatch):
    O, calls, _ = _lessons_spy(monkeypatch)
    monkeypatch.setattr(settings, "agentic_lessons_enabled", False, raising=False)
    asyncio.run(O._distill_lessons(SimpleNamespace(commit=lambda: None), "r", outcome="failed"))
    assert calls == [], "no LLM call at all when disabled"


def _drive_until(monkeypatch, final_status):
    """Drive the real drive_run over a stubbed _step that lands `final_status`, with the
    lease / heartbeat / preflight machinery stubbed out. Returns the distill calls."""
    import threading

    from app.agents import agentic_orchestrator as O
    from app.models.agentic import AgenticStatus
    seen = []

    run = SimpleNamespace(id="r", phase="code_change", status="running",
                          change_request_id="cr", kind="code", selected_repo_ids=[],
                          attempts_json={}, error=None, error_code=None, handoff_json={})
    db = SimpleNamespace(get=lambda _m, _i: run, commit=lambda: None,
                         rollback=lambda: None, refresh=lambda _r: None, add=lambda _r: None)

    async def _step(db_, run_, art, model):
        run_.status = final_status            # as mark_terminal(FAILED) does, mid-loop
    monkeypatch.setattr(O, "_step", _step)
    monkeypatch.setattr(O, "_rehydrate_art", lambda *a, **k: None)
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O, "_start_heartbeat",
                        lambda rid, owner: (threading.Event(), threading.Event()))
    monkeypatch.setattr(O.S, "acquire_lease", lambda *a, **k: True)
    monkeypatch.setattr(O.S, "renew_lease", lambda *a, **k: None)
    monkeypatch.setattr(O.S, "release_lease", lambda *a, **k: None)
    monkeypatch.setattr(O.S, "check_cancel", lambda _r: False)
    monkeypatch.setattr("app.agents.codegen_preflight.check_dependencies", lambda **k: [])
    monkeypatch.setattr(settings, "agentic_token_budget_hard_cap", 0)

    async def spy(db_, run_id, *, outcome):
        seen.append((run_id, outcome))
    monkeypatch.setattr(O, "_distill_lessons", spy)

    asyncio.run(O.drive_run(db, "r", intent="x"))
    assert AgenticStatus.FAILED.value == "failed"       # pin the literal the tail compares
    return seen


def test_drive_run_distills_once_on_a_terminal_failed_status(monkeypatch):
    # The choke point: every mark_terminal(FAILED) inside the phase loop breaks out and
    # falls through to drive_run's exit, so none of those sites needs its own call.
    assert _drive_until(monkeypatch, "failed") == [("r", "failed")]


def test_drive_run_does_not_distill_at_the_exit_for_a_completed_run(monkeypatch):
    # COMPLETED is distilled at the push, where the result is actually known — a second
    # call here would double-distill every successful run.
    assert _drive_until(monkeypatch, "completed") == []


# ── W5: stop report ──────────────────────────────────────────────────────────────

def _report(monkeypatch, *, planned=("src/A.java", "src/B.java"), touched=("src/A.java",),
            verification=None, green=None, facts=0, executions=1,
            reason="continuations_exhausted", rounds=3, agent_plan=None):
    from app.agents import agentic_orchestrator as O
    ta = {"files_to_modify": [{"path": p, "intent": "modify"} for p in planned]} if planned else {}
    monkeypatch.setattr(O, "_latest_ratified_analysis",
                        lambda db, cr: SimpleNamespace(technical_analysis=ta) if planned else None)
    run = SimpleNamespace(
        id="run-x", change_request_id="cr-x", selected_repo_ids=[],
        handoff_json={"facts": [{"fact": f"f{i}"} for i in range(facts)]})
    if green is not None:
        run.handoff_json["last_green_ckpt"] = green
    art = {"executions": executions}
    if verification is not None:
        art["verification"] = verification
    cs = SimpleNamespace(operations=[SimpleNamespace(path=p, op="modify") for p in touched],
                         plan=agent_plan or {})
    return O._stop_report(SimpleNamespace(), run, art, cs, reason=reason, rounds=rounds)


def test_stop_report_names_the_planned_files_that_were_never_touched(monkeypatch):
    rec = _report(monkeypatch, verification={"status": "needs_fix", "errors": ["a", "b"]})
    assert rec["planned_source"] == "ratified_plan"
    assert rec["planned"] == 2 and rec["touched"] == 1
    assert rec["planned_not_touched"] == ["src/B.java"]
    assert rec["planned_not_touched_total"] == 1
    assert rec["verify_status"] == "needs_fix" and rec["verify_errors"] == 2
    assert "ran out of continuation rounds after 3 round(s)" in rec["action"]
    assert "1 planned file(s) NEVER touched" in rec["action"]
    assert "last build FAILED (2 error(s))" in rec["action"]


def test_stop_report_distinguishes_never_built_from_built_and_failed(monkeypatch):
    never = _report(monkeypatch, verification=None)
    assert never["verify_status"] == "never_built"
    assert "NEVER BUILT — trust nothing here until it is verified" in never["action"]
    failed = _report(monkeypatch, verification={"status": "needs_fix", "errors": []})
    assert "NEVER BUILT" not in failed["action"]


def test_stop_report_says_so_when_no_green_was_ever_reached(monkeypatch):
    assert "NO green build was ever reached" in _report(monkeypatch)["action"]
    with_green = _report(monkeypatch, green={"label": "r2", "at": "t", "files": 5})
    assert with_green["last_green"] == {"label": "r2", "at": "t", "files": 5}
    assert "last green at r2 (5 file(s))" in with_green["action"]
    unlabelled = _report(monkeypatch, green={"label": None, "at": "t", "files": 5})
    assert "not to a round label" in unlabelled["action"]


def test_stop_report_flags_work_the_plan_never_asked_for(monkeypatch):
    rec = _report(monkeypatch, planned=("src/A.java",), touched=("src/A.java", "src/Z.java"))
    assert rec["touched_not_planned"] == ["src/Z.java"]
    assert rec["planned_not_touched"] == [] and "every planned file was touched" in rec["action"]


def test_stop_report_falls_back_to_the_agents_own_plan_then_admits_it_has_none(monkeypatch):
    rec = _report(monkeypatch, planned=(), touched=("src/A.java",),
                  agent_plan={"files": [{"path": "src/A.java"}, {"path": "src/C.java"}]})
    assert rec["planned_source"] == "agent_plan" and rec["planned_not_touched"] == ["src/C.java"]
    blind = _report(monkeypatch, planned=(), touched=("src/A.java",), agent_plan={})
    assert blind["planned_source"] == "none"
    assert "no plan on record to compare against" in blind["action"]


def test_stop_report_matches_paths_by_basename(monkeypatch):
    # The plan names a full repo path, the disk change-set is repo-relative — the same
    # coarse matching the review floor and plan fidelity already use.
    rec = _report(monkeypatch, planned=("core/src/main/java/A.java",), touched=("src/A.java",))
    assert rec["planned_not_touched"] == [] and rec["touched_not_planned"] == []


def test_stop_report_caps_file_lists_and_names_the_truncation(monkeypatch):
    rec = _report(monkeypatch, planned=tuple(f"src/F{i}.java" for i in range(60)), touched=())
    assert len(rec["planned_not_touched"]) == 40
    assert rec["planned_not_touched_total"] == 60, "the totals stay exact"
    assert "capped at 40" in rec["truncated"], "a truncated report must never read as complete"


def test_stop_report_reports_an_author_that_never_built_its_own_work(monkeypatch):
    assert "the agent never ran a build or test itself" in _report(monkeypatch, executions=0)["action"]
    assert "never ran a build" not in _report(monkeypatch, executions=3)["action"]


def test_stop_report_counts_the_facts_carried_out_of_the_run(monkeypatch):
    assert _report(monkeypatch, facts=4)["facts"] == 4


def test_every_non_complete_stop_reason_has_human_wording():
    from app.agents.agentic_orchestrator import _STOP_REASON_WORDS, _stop_report_line
    for reason in ("budget_exceeded", "no_progress", "continuations_exhausted", "loop_capped"):
        line = _stop_report_line({"reason": reason, "rounds": 1, "touched": 0,
                                  "planned_source": "none", "verify_status": "never_built"})
        assert _STOP_REASON_WORDS[reason] in line
    # an unknown reason degrades to the raw token rather than vanishing
    assert "something_new" in _stop_report_line(
        {"reason": "something_new", "rounds": 1, "touched": 0,
         "planned_source": "none", "verify_status": "never_built"})


def test_phase_code_persists_and_emits_the_stop_report_only_when_it_stopped_short(monkeypatch):
    from app.agents import agentic_orchestrator as O
    from app.agents.agentic_subagents import ChangeSet

    def _drive(stopped, max_cont):
        events = []
        monkeypatch.setattr(O, "emit_event",
                            lambda db, rid, kind, payload=None: events.append((kind, payload)))
        monkeypatch.setattr(O, "_disk_change_count", lambda _run: 1)
        monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(
            operations=[SimpleNamespace(path="src/A.java", op="modify")]))
        monkeypatch.setattr(O, "_latest_ratified_analysis", lambda db, cr: SimpleNamespace(
            technical_analysis={"files_to_modify": [{"path": "src/A.java"},
                                                    {"path": "src/B.java"}]}))
        monkeypatch.setattr(settings, "use_self_correction", False)
        monkeypatch.setattr(settings, "agentic_max_code_continuations", max_cont)

        async def fake(db, **kw):
            return ChangeSet(operations=[SimpleNamespace(path="src/A.java")],
                             stopped=stopped, iterations=60, executions=0)
        monkeypatch.setattr(O.agentic_subagents, "run_code_change", fake)
        run = SimpleNamespace(id="run-x", change_request_id="cr-x", selected_repo_ids=[],
                              attempts_json={}, handoff_json={}, parent_run_id=None)
        db = SimpleNamespace(add=lambda _r: None, commit=lambda: None, rollback=lambda: None,
                             get=lambda _m, _i: run)
        art = {"ctx": None, "intent": "x"}
        asyncio.run(O._phase_code(db, run, art, None))
        return run, art, events

    run, art, events = _drive("max_iterations", 0)        # capped, no continuations left
    reports = [p for k, p in events if k == "stop_report"]
    assert reports and reports[0]["reason"] == "continuations_exhausted"
    assert reports[0]["planned_not_touched"] == ["src/B.java"]
    assert run.handoff_json["stop_report"] == reports[0], "survives event scroll + resume"
    assert art["stop_report"] == reports[0]

    run2, _, events2 = _drive("completed", 2)             # finished clean → no report
    assert not any(k == "stop_report" for k, _ in events2)
    assert "stop_report" not in run2.handoff_json


def test_run_view_surfaces_the_stop_report(monkeypatch):
    import app.api.agentic as A
    monkeypatch.setattr(A, "_push_view", lambda run: {})
    rec = {"reason": "budget_exceeded", "action": "⏹ …"}
    run = SimpleNamespace(
        id="r", change_request_id="c", phase="verify", status="running",
        manifest_hash=None, cancel_requested=False, error=None, error_code=None,
        created_by=None, kind="code", parent_run_id=None, selected_repo_ids=[],
        handoff_json={"stop_report": rec})
    assert A._run_view(run)["stop_report"] == rec
    run.handoff_json = {}
    assert A._run_view(run)["stop_report"] is None, "a clean phase reports nothing"


# ── W6: green-state preservation ─────────────────────────────────────────────────

def _op(path, content="x", op="modify", repo="repo-1"):
    return SimpleNamespace(op=op, repo_id=repo, path=path, content=content,
                           content_hash="h")


def _snap(monkeypatch, ops, *, cap=4_000_000, base_sha="abc123"):
    from app.agents import agentic_orchestrator as O
    events = []
    monkeypatch.setattr(O, "emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: SimpleNamespace(operations=ops))
    monkeypatch.setattr(O, "_ws_id", lambda run: "ws-1")
    monkeypatch.setattr(O.workspace_local, "read_base_sha", lambda ws, rid: base_sha)
    monkeypatch.setattr(settings, "agentic_green_snapshot_max_bytes", cap)
    run = SimpleNamespace(id="run-x", selected_repo_ids=["repo-1"],
                          handoff_json={"feature_branch": "feat/x"})
    rec = O._persist_green_change_set(None, run)
    return run, rec, events


def test_a_green_verify_makes_the_change_set_durable(monkeypatch):
    run, rec, events = _snap(monkeypatch, [_op("src/A.java", "class A {}"),
                                           _op("src/B.java", "class B {}")])
    assert rec["complete"] is True and rec["files"] == 2
    assert rec["base_sha"] == {"repo-1": "abc123"}, "pins the base the change was built on"
    assert rec["feature_branch"] == "feat/x"
    assert {o["path"] for o in rec["ops"]} == {"src/A.java", "src/B.java"}
    assert any(o["content"] == "class A {}" for o in rec["ops"]), "content, not just paths"
    assert run.handoff_json["green_change_set"] == rec
    assert "survives a workspace cleanup" in [p for k, p in events
                                              if k == "green_snapshot"][0]["action"]


def test_snapshot_records_deletes_without_content(monkeypatch):
    _, rec, _ = _snap(monkeypatch, [_op("src/Gone.java", None, op="delete")])
    assert rec["ops"] == [{"op": "delete", "repo_id": "repo-1", "path": "src/Gone.java"}]
    assert rec["complete"] is True


def test_an_oversized_change_set_is_marked_incomplete_and_names_what_was_dropped(monkeypatch):
    _, rec, events = _snap(monkeypatch, [_op("src/A.java", "a" * 100),
                                         _op("src/Big.java", "b" * 5000)], cap=1000)
    assert rec["complete"] is False and rec["files"] == 1
    assert rec["omitted_total"] == 1 and "src/Big.java" in rec["omitted"][0]
    assert "5,000 bytes" in rec["omitted"][0], "says HOW big, so the cap can be set"
    payload = [p for k, p in events if k == "green_snapshot"][0]
    assert payload["complete"] is False
    assert "only PARTLY saved" in payload["action"]
    assert "AGENTIC_GREEN_SNAPSHOT_MAX_BYTES" in payload["action"], "names the knob"


def test_snapshot_ordering_is_deterministic_so_omissions_are_reproducible(monkeypatch):
    ops = [_op("src/Z.java", "z" * 400), _op("src/A.java", "a" * 400)]
    _, first, _ = _snap(monkeypatch, ops, cap=500)
    _, second, _ = _snap(monkeypatch, list(reversed(ops)), cap=500)
    assert [o["path"] for o in first["ops"]] == [o["path"] for o in second["ops"]] \
        == ["src/A.java"], "same tree → same snapshot, regardless of walk order"


def test_snapshots_can_be_disabled_and_an_empty_tree_snapshots_nothing(monkeypatch):
    _, rec, events = _snap(monkeypatch, [_op("src/A.java")], cap=0)
    assert rec is None and not events
    _, empty, _ = _snap(monkeypatch, [])
    assert empty is None


def test_a_snapshot_failure_never_fails_a_green_build(monkeypatch):
    from app.agents import agentic_orchestrator as O
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O, "_disk_change_set", lambda db, run: 1 / 0)
    monkeypatch.setattr(settings, "agentic_green_snapshot_max_bytes", 1000)
    assert O._persist_green_change_set(None, SimpleNamespace(
        id="r", selected_repo_ids=[], handoff_json={})) is None


def _rebuild(monkeypatch, tmp_path, handoff, *, cloned=True, mat=None):
    from app.agents import agentic_orchestrator as O
    events, calls = [], []
    rd = tmp_path / "ws-1" / "repo-1"
    rd.mkdir(parents=True, exist_ok=True)
    if cloned:
        (rd / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(O, "emit_event",
                        lambda db, rid, kind, payload=None: events.append((kind, payload)))
    monkeypatch.setattr(O.repo_scope, "validate_selection", lambda db, ids: [
        SimpleNamespace(id="repo-1", gitlab_repo="grp/repo", gitlab_branch="main",
                        gitlab_url=None)])
    monkeypatch.setattr(O.workspace_local, "repo_dir", lambda ws, rid: rd)
    monkeypatch.setattr(O.workspace_local, "clone",
                        lambda *a, **k: calls.append("clone") or "sha")
    monkeypatch.setattr(O.workspace_local, "create_branch", lambda *a, **k: None)
    monkeypatch.setattr(O.workspace_local, "set_remote", lambda *a, **k: None)
    monkeypatch.setattr(O.workspace_local, "build_clone_url", lambda *a, **k: "url")
    monkeypatch.setattr(O.workspace_local, "reset_to_base",
                        lambda ws, rid, sha: calls.append(f"reset:{sha}"))

    def _mat(ws, rid, files):
        for f in files:
            p = rd / f["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f["content"])
        return len(files)
    monkeypatch.setattr(O.workspace_local, "materialize_files", mat or _mat)
    run = SimpleNamespace(id="run-x", selected_repo_ids=["repo-1"], handoff_json=handoff,
                          change_request_id="cr")
    return O._rematerialize_green(None, run, "ws-1"), rd, events, calls


def test_a_lost_workspace_is_rebuilt_from_the_snapshot(monkeypatch, tmp_path):
    handoff = {"green_change_set": {
        "complete": True, "base_sha": {"repo-1": "base9"}, "feature_branch": "feat/x",
        "ops": [{"op": "modify", "repo_id": "repo-1", "path": "src/A.java",
                 "content": "class A {}"}]}}
    res, rd, events, calls = _rebuild(monkeypatch, tmp_path, handoff, cloned=False)
    assert res["ok"] is True and res["files"] == 1
    assert (rd / "src" / "A.java").read_text() == "class A {}"
    assert "clone" in calls, "no .git → re-clone"
    assert "reset:base9" in calls, "pins the base the verified change was built on"
    assert "no re-run needed" in [p for k, p in events if k == "green_rematerialized"][0]["action"]


def test_rebuild_reapplies_deletions_a_fresh_clone_would_restore(monkeypatch, tmp_path):
    # The verified tree had this file REMOVED. A clone brings it back, so the rebuild
    # has to delete it again or the pushed change silently gains a file back.
    handoff = {"green_change_set": {
        "complete": True, "base_sha": {}, "ops": [
            {"op": "delete", "repo_id": "repo-1", "path": "src/Gone.java"}]}}
    rd = tmp_path / "ws-1" / "repo-1"
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "Gone.java").write_text("class Gone {}")
    res, rd, _, _ = _rebuild(monkeypatch, tmp_path, handoff)
    assert res["ok"] is True
    assert not (rd / "src" / "Gone.java").exists()


def test_rebuild_tolerates_an_already_absent_deletion(monkeypatch, tmp_path):
    handoff = {"green_change_set": {
        "complete": True, "base_sha": {}, "ops": [
            {"op": "delete", "repo_id": "repo-1", "path": "src/NeverThere.java"}]}}
    assert _rebuild(monkeypatch, tmp_path, handoff)[0]["ok"] is True


def test_rebuild_refuses_an_incomplete_snapshot_rather_than_pushing_a_subset(monkeypatch, tmp_path):
    handoff = {"green_change_set": {"complete": False, "omitted_total": 3, "ops": [
        {"op": "modify", "repo_id": "repo-1", "path": "src/A.java", "content": "a"}]}}
    res, _, _, _ = _rebuild(monkeypatch, tmp_path, handoff)
    assert res["ok"] is False
    assert "INCOMPLETE" in res["reason"] and "3 file(s)" in res["reason"]
    assert "only part of a verified change" in res["reason"]
    assert "AGENTIC_GREEN_SNAPSHOT_MAX_BYTES" in res["reason"]


def test_rebuild_refuses_when_nothing_was_ever_saved(monkeypatch, tmp_path):
    res, _, _, _ = _rebuild(monkeypatch, tmp_path, {})
    assert res["ok"] is False and "no verified change-set was saved" in res["reason"]
    assert "re-run the change" in res["reason"]


def test_rebuild_refuses_on_a_short_write_rather_than_claiming_success(monkeypatch, tmp_path):
    from app.agents import agentic_orchestrator as O
    handoff = {"green_change_set": {"complete": True, "base_sha": {}, "ops": [
        {"op": "modify", "repo_id": "repo-1", "path": "src/A.java", "content": "a"},
        {"op": "modify", "repo_id": "repo-1", "path": "src/B.java", "content": "b"}]}}
    assert _rebuild(monkeypatch, tmp_path, handoff)[0]["ok"] is True      # control
    # Now let the write come up short — the same shape as the Phase-A XSD restore guard.
    res, _, _, _ = _rebuild(monkeypatch, tmp_path, handoff,
                            mat=lambda ws, rid, files: 1)
    assert res["ok"] is False and "rebuilt only 1 of 2" in res["reason"]
    assert "re-run the change rather than trusting it" in res["reason"]


def test_rebuild_never_raises(monkeypatch, tmp_path):
    from app.agents import agentic_orchestrator as O
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O.repo_scope, "validate_selection", lambda db, ids: 1 / 0)
    res = O._rematerialize_green(None, SimpleNamespace(
        id="r", selected_repo_ids=["repo-1"], change_request_id="cr",
        handoff_json={"green_change_set": {"complete": True, "ops": [{"path": "a"}]}}), "ws")
    assert res["ok"] is False and "failed" in res["reason"]


def test_reverify_rebuilds_a_collected_workspace_instead_of_giving_up(monkeypatch):
    from app.agents import agentic_orchestrator as O
    run = SimpleNamespace(id="r", handoff_json={}, selected_repo_ids=["repo-1"])
    monkeypatch.setattr(O, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(O.workspace_local, "run_dir",
                        lambda ws: SimpleNamespace(exists=lambda: False))
    monkeypatch.setattr(O, "_ws_id", lambda r: "ws")

    db = SimpleNamespace(get=lambda _m, _i: run, commit=lambda: None)
    # refusal → still `expired`, but carrying the precise reason
    monkeypatch.setattr(O, "_rematerialize_green",
                        lambda db_, r, ws: {"ok": False, "files": 0,
                                            "reason": "the saved change-set is INCOMPLETE"})
    out = O.reverify_run(db, "r")
    assert out["status"] == "expired" and "INCOMPLETE" in out["reason"]
    assert "cleaned up" in out["reason"]

    # success → the real build runs against the rebuilt tree
    monkeypatch.setattr(O, "_rematerialize_green",
                        lambda db_, r, ws: {"ok": True, "files": 2, "reason": ""})
    monkeypatch.setattr(O, "_disk_change_set", lambda db_, r: SimpleNamespace(
        operations=[_op("src/A.java")]))
    monkeypatch.setattr(O, "_phase_verify", lambda db_, r, art: art.setdefault(
        "verification", {"status": "verified", "modules": {}, "reason": ""}) and "verified")
    assert O.reverify_run(db, "r")["status"] == "verified"


def test_run_view_reports_snapshot_durability_without_the_file_contents(monkeypatch):
    import app.api.agentic as A
    monkeypatch.setattr(A, "_push_view", lambda run: {})
    run = SimpleNamespace(
        id="r", change_request_id="c", phase="verify", status="running",
        manifest_hash=None, cancel_requested=False, error=None, error_code=None,
        created_by=None, kind="code", parent_run_id=None, selected_repo_ids=[],
        handoff_json={"green_change_set": {
            "at": "t", "files": 2, "bytes": 99, "complete": True, "omitted": [],
            "omitted_total": 0,
            "ops": [{"path": "a", "content": "x" * 10_000}]}})
    view = A._run_view(run)["green_snapshot"]
    assert view == {"at": "t", "files": 2, "bytes": 99, "complete": True,
                    "omitted": [], "omitted_total": 0}
    assert "ops" not in view, "megabytes of file content never ride a status response"
    run.handoff_json = {}
    assert A._run_view(run)["green_snapshot"] is None
