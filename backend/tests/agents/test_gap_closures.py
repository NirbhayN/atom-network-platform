# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Gap closures G2–G7: lessons memory, incremental verify, scratch runner,
operator hooks, round checkpoints, context-savings metrics."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings


# ── G2: lessons merge/parse (pure) ──────────────────────────────────────────────────

def test_lessons_merge_dedupes_and_caps():
    from app.services.agentic_lessons import merge_lessons
    existing = "- The IGW module needs JDK 8\n- Regenerate JAXB after any XSD edit"
    merged, added = merge_lessons(existing, [
        "the igw module needs jdk 8",                 # dup (normalized)
        "gateway-web depends on service-mesh-common — build it first",
    ], cap=40)
    assert added == 1 and "service-mesh-common" in merged
    merged2, _ = merge_lessons(merged, [f"lesson {i}" for i in range(50)], cap=10)
    assert len(merged2.splitlines()) == 10, "capped FIFO — newest survive"
    assert "lesson 49" in merged2 and "JDK 8" not in merged2


# ── G3: round-aware clean ───────────────────────────────────────────────────────────

def test_mvn_clean_override_controls_the_clean_goal(monkeypatch):
    from app.agents import verification_plan as V
    monkeypatch.setattr(settings, "agentic_verify_clean", True)
    assert "clean" in V._mvn("install")
    assert "clean" not in V._mvn("install", clean=False)      # incremental round
    assert "clean" in V._mvn("install", clean=True)
    assert "clean" not in V._mvn_module("install", "m", clean=False)


# ── G4: scratch runner ──────────────────────────────────────────────────────────────

def test_scratch_run_disabled_by_default(ws_tools):
    import app.agents.agentic_tools as T
    with pytest.raises(T.ToolError) as e:
        T.scratch_run(ws_tools, script="print('hi')")
    assert "DISABLED" in str(e.value)


def test_scratch_run_executes_contained_script(ws_tools, monkeypatch):
    import app.agents.agentic_tools as T
    monkeypatch.setattr(settings, "agentic_scratch_enabled", True, raising=False)
    out = T.scratch_run(ws_tools, script=(
        "import json, os\n"
        "repos = json.load(open('repos.json'))\n"
        "print('repos:', len(repos))\n"
        "print(open(list(repos.values())[0] + '/src/A.java').read().strip().splitlines()[0])\n"))
    assert "exit=0" in out and "repos: 1" in out and "class A" in out


def test_scratch_run_rejects_empty_and_huge_scripts(ws_tools, monkeypatch):
    import app.agents.agentic_tools as T
    monkeypatch.setattr(settings, "agentic_scratch_enabled", True, raising=False)
    with pytest.raises(T.ToolError):
        T.scratch_run(ws_tools, script="   ")
    with pytest.raises(T.ToolError):
        T.scratch_run(ws_tools, script="x = 1\n" * 4000)


@pytest.fixture
def ws_tools(tmp_path, monkeypatch):
    import app.agents.agentic_tools as T
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-1" / "repo-1"
    (rd / "src").mkdir(parents=True)
    (rd / "src" / "A.java").write_text("class A {\n    int x = 1;\n}\n")
    return T.RunContext(run_id="run-1", selected_repo_ids=["repo-1"])


# ── G5: operator hooks ──────────────────────────────────────────────────────────────

def test_pre_hook_exit_2_blocks_with_its_stdout(tmp_path, monkeypatch):
    from app.agents import agentic_runtime as R
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\necho 'edits to prod configs are forbidden here'\nexit 2\n")
    hook.chmod(0o755)
    monkeypatch.setattr(settings, "agentic_hook_pre_tool", str(hook), raising=False)
    msg = R._run_tool_hook("pre", run_id="r", agent="code_change", iteration=1,
                           tool="edit_file", tool_input={"path": "x"})
    assert msg == "edits to prod configs are forbidden here"


def test_hooks_fail_open(tmp_path, monkeypatch):
    from app.agents import agentic_runtime as R
    monkeypatch.setattr(settings, "agentic_hook_pre_tool", "/nonexistent/hook", raising=False)
    assert R._run_tool_hook("pre", run_id="r", agent="a", iteration=1,
                            tool="grep", tool_input={}) is None
    monkeypatch.setattr(settings, "agentic_hook_pre_tool", "", raising=False)
    assert R._run_tool_hook("pre", run_id="r", agent="a", iteration=1,
                            tool="grep", tool_input={}) is None


# ── G6: round checkpoints + revert ──────────────────────────────────────────────────

def _git_repo(root: Path):
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "Keep.java").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=root, check=True)


def test_checkpoint_and_revert_roundtrip(tmp_path, monkeypatch):
    from app.agents import round_checkpoint as CK
    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    rd = tmp_path / "run-1" / "repo-1"
    _git_repo(rd)
    (rd / "Untracked.java").write_text("pre-round work\n")      # agent file from round 1

    shas = CK.snapshot("run-1", "run-1", ["repo-1"], "r2")
    assert "repo-1" in shas
    # worktree/index untouched by the snapshot
    st = subprocess.run(["git", "status", "--porcelain"], cwd=rd,
                        capture_output=True, text=True).stdout
    assert "?? Untracked.java" in st

    # round 2 goes bad: mutates a tracked file, mutates round-1 work, adds junk
    (rd / "Keep.java").write_text("broken by round 2\n")
    (rd / "Untracked.java").write_text("clobbered\n")
    (rd / "Junk.java").write_text("should disappear\n")

    out = CK.revert("run-1", "run-1", shas)
    assert out["reverted"] and not out["failed"]
    assert (rd / "Keep.java").read_text() == "original\n"
    assert (rd / "Untracked.java").read_text() == "pre-round work\n"
    assert not (rd / "Junk.java").exists(), "post-checkpoint files are removed"


# ── G7: metrics aggregation of the new signals ──────────────────────────────────────

def test_metrics_count_scout_and_reclaimed_context():
    from app.services.agentic_metrics import summarize_runs
    runs = [{"id": "r1", "status": "completed", "kind": "code"}]
    events = {"r1": [
        {"kind": "scout_done", "payload": {"ok": True}},
        {"kind": "scout_done", "payload": {"ok": False}},
        {"kind": "history_compacted", "payload": {"reclaimed_chars": 5000}},
        {"kind": "reads_superseded", "payload": {"chars": 1200}},
        {"kind": "verification", "payload": {"status": "verified"}},
    ]}
    out = summarize_runs(runs, events)
    assert out["scout_runs"] == 2 and out["scout_ok"] == 1
    assert out["context_reclaimed_chars"] == 6200
