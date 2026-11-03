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
