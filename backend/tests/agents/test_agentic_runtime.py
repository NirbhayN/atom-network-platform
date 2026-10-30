# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""The bounded agentic loop (§8) with the LLM scripted via a fake call_messages_api_tools."""
import asyncio
import subprocess

import pytest

from app.core.config import settings
from app.core.llm import ToolUseRequest, MessagesToolTurn
from app.agents import agentic_runtime as R

RID = "repo-1"
RUN = "run-1"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / RUN / RID
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    subprocess.run(["git", "init", "-q"], cwd=rd, check=True)
    subprocess.run(["git", "add", "-A"], cwd=rd, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=rd, check=True)
    return rd


def _turn(tool_uses, text="", stop="tool_use"):
    ac = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input} for tu in tool_uses
    ]
    return MessagesToolTurn(text=text, tool_uses=tool_uses, stop_reason=stop, assistant_content=ac)


def _script_runner(monkeypatch, script):
    state = {"i": 0}

    async def fake(**kwargs):
        t = script[state["i"]]
        state["i"] += 1
        return t

    monkeypatch.setattr(R, "call_messages_api_tools", fake)


def test_full_loop_plan_read_edit_finish(ws, monkeypatch):
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "submit_plan", {"summary": "bump x"})]),
        _turn([ToolUseRequest("t2", "read_file", {"repo_id": RID, "path": "src/A.java"})]),
        # §8 gate: a .java edit needs structural intel first — ast_query satisfies it
        _turn([ToolUseRequest("t2b", "ast_query", {"repo_id": RID, "path": "src/A.java"})]),
        _turn([ToolUseRequest("t3", "edit_file", {"repo_id": RID, "path": "src/A.java",
                                                  "old_string": "int x = 1;", "new_string": "int x = 2;"})]),
        _turn([], text="done", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))

    assert res.stopped == "completed" and res.iterations == 5
    assert res.plan["summary"] == "bump x"
    assert len(res.change_set) == 1 and res.change_set[0].op == "modify"
    assert "int x = 2;" in (ws / "src" / "A.java").read_text()


def test_mutation_blocked_before_plan(ws, monkeypatch):
    # read is fine, but edit before submit_plan is refused (turn-1 enforcement).
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "read_file", {"repo_id": RID, "path": "src/A.java"})]),
        _turn([ToolUseRequest("t2", "edit_file", {"repo_id": RID, "path": "src/A.java",
                                                  "old_string": "int x = 1;", "new_string": "int x = 9;"})]),
        _turn([], text="stopped", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.change_set == []                              # edit refused
    assert "int x = 1;" in (ws / "src" / "A.java").read_text()  # file untouched


def test_iteration_cap_stops_the_loop(ws, monkeypatch):
    # Always asks for a tool → must stop at the cap, not spin forever.
    always = _turn([ToolUseRequest("t", "grep", {"repo_id": RID, "pattern": "x"})])

    async def fake(**kwargs):
        return always
    monkeypatch.setattr(R, "call_messages_api_tools", fake)

    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=3))
    assert res.stopped == "max_iterations" and res.iterations == 3


def test_events_emitted_for_turns_and_tools(ws, monkeypatch):
    events = []
    monkeypatch.setattr(R, "emit_event",
                        lambda db, run_id, kind, payload: events.append((kind, payload)))
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "submit_plan", {"summary": "x"})]),
        _turn([], text="done", stop="end_turn"),
    ])
    asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=object()))
    kinds = [k for k, _ in events]
    assert kinds.count("llm_turn") == 2
    assert ("tool_call" in kinds) and ("loop_done" in kinds)


def test_empty_assistant_turn_ends_loop_without_crashing(ws, monkeypatch):
    # stop_reason='tool_use' but NO blocks → would make the next request's
    # assistant content empty (Anthropic 400). The loop must end cleanly instead.
    bad = MessagesToolTurn(text="", tool_uses=[], stop_reason="tool_use", assistant_content=[])

    async def fake(**kwargs):
        return bad
    monkeypatch.setattr(R, "call_messages_api_tools", fake)

    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=5))
    assert res.stopped == "completed" and res.iterations == 1


def test_final_text_carried_on_cap(ws, monkeypatch):
    t = _turn([ToolUseRequest("t", "grep", {"repo_id": RID, "pattern": "x"})], text="still working")

    async def fake(**kwargs):
        return t
    monkeypatch.setattr(R, "call_messages_api_tools", fake)

    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, max_iterations=2))
    assert res.stopped == "max_iterations" and res.final_text == "still working"


def test_cancel_check_stops_before_first_turn(ws, monkeypatch):
    _script_runner(monkeypatch, [_turn([], text="never", stop="end_turn")])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go",
        db=None, cancel_check=lambda: True))
    assert res.stopped == "cancelled" and res.iterations == 0


def test_bounced_plan_keeps_mutations_blocked(ws, monkeypatch):
    # A submit_plan whose files fail validation is NOT recorded — turn-1 enforcement
    # keeps refusing edits until a well-formed plan lands, so a malformed plan can
    # never become a licence to mutate.
    _script_runner(monkeypatch, [
        _turn([ToolUseRequest("t1", "submit_plan",
                              {"summary": "drift",
                               "files": [{"file": "src/A.java", "action": "edit"}]})]),
        _turn([ToolUseRequest("t2", "edit_file", {"repo_id": RID, "path": "src/A.java",
                                                  "old_string": "int x = 1;",
                                                  "new_string": "int x = 9;"})]),
        _turn([], text="stopped", stop="end_turn"),
    ])
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.plan is None and res.change_set == []
    assert "int x = 1;" in (ws / "src" / "A.java").read_text()


# ── Context-discipline invariants: compaction + supersede-aware eviction ──────────

def test_compact_messages_never_touches_brief_or_assistant_turns():
    big = "x" * 1000
    msgs = [
        {"role": "user", "content": "THE BRIEF " + big},
        {"role": "assistant", "content": [{"type": "text", "text": "reasoning " + big}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}]},
        {"role": "assistant", "content": [{"type": "text", "text": "later"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": big}]},
    ]
    reclaimed = R._compact_messages(msgs, keep_tail=1)
    assert reclaimed >= 1000
    assert msgs[0]["content"].startswith("THE BRIEF"), "the brief is never evicted"
    assert "reasoning" in msgs[1]["content"][0]["text"], "assistant reasoning is never evicted"
    assert msgs[2]["content"][0]["content"].startswith("[evicted ")
    assert msgs[4]["content"][0]["content"] == big, "kept tail stays verbatim"


def test_evict_superseded_reads_stubs_only_that_paths_reads():
    big = "y" * 500
    msgs = [
        {"role": "user", "content": "brief"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "read_file",
             "input": {"repo_id": "repo-1", "path": "src/A.java"}},
            {"type": "tool_use", "id": "r2", "name": "read_file",
             "input": {"path": "src/A.java"}},                      # resolver-recovered repo
            {"type": "tool_use", "id": "r3", "name": "read_file",
             "input": {"repo_id": "repo-1", "path": "src/B.java"}},
            {"type": "tool_use", "id": "g1", "name": "grep",
             "input": {"repo_id": "repo-1", "pattern": "x"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": big},
            {"type": "tool_result", "tool_use_id": "r2", "content": big},
            {"type": "tool_result", "tool_use_id": "r3", "content": big},
            {"type": "tool_result", "tool_use_id": "g1", "content": big}]},
    ]
    reclaimed = R._evict_superseded_reads(msgs, "repo-1", "src/A.java")
    assert reclaimed == 1000                      # both A.java reads, nothing else
    blocks = msgs[2]["content"]
    assert blocks[0]["content"].startswith("[evicted") and blocks[1]["content"].startswith("[evicted")
    assert blocks[2]["content"] == big and blocks[3]["content"] == big


def test_evict_superseded_reads_respects_floor_and_prior_stubs():
    small = "z" * 100                             # under the eviction floor — not worth stubbing
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "read_file",
             "input": {"repo_id": "repo-1", "path": "src/A.java"}},
            {"type": "tool_use", "id": "r2", "name": "read_file",
             "input": {"repo_id": "repo-1", "path": "src/A.java"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": small},
            {"type": "tool_result", "tool_use_id": "r2", "content": "[evicted earlier tool output]"}]},
    ]
    assert R._evict_superseded_reads(msgs, "repo-1", "src/A.java") == 0
    assert msgs[1]["content"][0]["content"] == small


def test_loop_edit_supersedes_earlier_read_in_history(ws, monkeypatch):
    # End-to-end: after the edit lands, the earlier read_file result in the live
    # message history is stubbed; the model keeps the edit echo, not the stale body.
    captured = {}

    async def fake(**kwargs):
        captured["messages"] = kwargs.get("messages")
        t = fake.script[fake.i]
        fake.i += 1
        return t
    # Big enough to clear the eviction floor — tiny reads are deliberately left alone.
    filler = "".join(f"    // context filler line {n}\n" for n in range(40))
    (ws / "src" / "Big.java").write_text("class Big {\n" + filler + "    int x = 1;\n}\n")
    fake.i = 0
    fake.script = [
        _turn([ToolUseRequest("t1", "submit_plan", {"summary": "bump x"})]),
        _turn([ToolUseRequest("t2", "read_file", {"repo_id": RID, "path": "src/Big.java"})]),
        _turn([ToolUseRequest("t3", "ast_query", {"repo_id": RID, "path": "src/Big.java"})]),
        _turn([ToolUseRequest("t4", "edit_file", {"repo_id": RID, "path": "src/Big.java",
                                                  "old_string": "int x = 1;", "new_string": "int x = 2;"})]),
        _turn([], text="done", stop="end_turn"),
    ]
    monkeypatch.setattr(R, "call_messages_api_tools", fake)
    res = asyncio.run(R.run_agent_loop(
        run_id=RUN, selected_repo_ids=[RID], system="s", user_prompt="go", db=None))
    assert res.stopped == "completed"
    # The final LLM call saw the read's result replaced by the supersede stub.
    read_results = [b for m in captured["messages"] if isinstance(m.get("content"), list)
                    for b in m["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") == "t2"]
    assert read_results and str(read_results[0]["content"]).startswith("[evicted")
