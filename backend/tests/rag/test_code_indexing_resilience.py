# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Resilience tests for the code-indexing pipeline.

Covers the fixes from wiki/tasks/code-indexing-orphan-fix.md:
  - `_JobHeartbeat` keeps a long ingest's `agent_jobs.updated_at` moving so
    the orphan sweeper spares live jobs (T1);
  - `on_progress` milestones flow out of `ingest_repo` and never abort the
    ingest when the sink raises (T4);
  - an empty fetch preserves the existing index instead of wiping it (T5);
  - a second index trigger while a job is active is rejected with 409,
    fail-open on guard errors (T3);
  - incremental fast path takes deletions from the Compare API only — the
    SHA-diff's `deleted` (= every unchanged file) must not wipe the index (T6).

House patterns from test_code_ingestion_lsp_wiring.py (stub fetcher/chunker/
embeds + fake DB; patch embed_texts at BOTH `ci.embed_texts` and
`app.rag.embeddings.embed_texts`) and test_code_ingestion_polyglot.py (call
endpoints directly with a fake DB + MagicMock user).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api import code_indexing as api
from app.core.config import settings
from app.rag import code_ingestion as ci


class _FakeDb:
    """Minimal DB stand-in for the ingest entrypoints: empty existing-chunk
    query, no-op writes."""

    def __init__(self):
        self.added: list = []

    def query(self, *a, **kw):
        class _Q:
            def filter(self, *a, **kw):
                return self

            def all(self):
                return []

        return _Q()

    def flush(self):
        pass

    def commit(self):
        pass

    def add(self, x):
        self.added.append(x)


# ──────────────────────────────────────────────────────────────────────────────
# T1 — _JobHeartbeat
# ──────────────────────────────────────────────────────────────────────────────

class TestJobHeartbeat:

    def _recording_hb(self, interval_s: float = 0.05):
        touches: list[tuple] = []
        hb = api._JobHeartbeat(
            "job-12345678", interval_s=interval_s,
            touch_fn=lambda stage, pct: touches.append((stage, pct)),
        )
        return hb, touches

    def test_beats_while_running_then_freezes_after_stop(self):
        hb, touches = self._recording_hb()
        hb.start()
        time.sleep(0.3)
        hb.stop()
        assert len(touches) >= 2, "heartbeat should have beaten several times"
        frozen = len(touches)
        time.sleep(0.15)
        assert len(touches) == frozen, "no beat may land after stop()"

    def test_progress_after_stop_writes_nothing(self):
        hb, touches = self._recording_hb()
        hb.start()
        hb.stop()
        frozen = len(touches)
        hb.progress("late stage", 99)
        assert len(touches) == frozen

    def test_progress_writes_through_and_next_beat_repeats_it(self):
        hb, touches = self._recording_hb()
        hb.start()
        hb.progress("s1", 10)
        time.sleep(0.2)
        hb.stop()
        assert touches[0] == ("s1", 10), "progress() must write through immediately"
        assert touches.count(("s1", 10)) >= 2, "the beat must repeat the last stage"

    def test_broken_touch_fn_never_raises(self):
        def boom(stage, pct):
            raise RuntimeError("db down")

        hb = api._JobHeartbeat("job-12345678", interval_s=0.05, touch_fn=boom)
        hb.start()
        hb.progress("s1", 1)          # must not raise
        time.sleep(0.12)
        hb.stop()                     # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# T4 — on_progress plumbing through ingest_repo
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def java_ingest_deps(monkeypatch):
    files = [{"path": "src/Main.java", "content": "class Main {}"}]
    monkeypatch.setattr(ci, "_fetch_java_files_from_gitlab",
                        lambda repo, branch: files)

    def fake_chunk_source_file(path, content, language, *, fallback=None):
        return [{
            "path": path, "class_name": "Main", "method_name": None,
            "content": content, "chunk_index": 0, "language": language,
        }]

    monkeypatch.setattr(ci.code_chunker_ts, "chunk_source_file", fake_chunk_source_file)

    _fake_embed = lambda texts: [[0.0] * 768 for _ in texts]  # noqa: E731
    monkeypatch.setattr(ci, "embed_texts", _fake_embed)
    monkeypatch.setattr("app.rag.embeddings.embed_texts", _fake_embed)
    monkeypatch.setattr(ci, "embed_chunks_with_cache",
                        lambda db, batch: [[0.0] * 768 for _ in batch])

    monkeypatch.setattr(settings, "use_symbol_graph_extractor", False)
    monkeypatch.setattr(settings, "use_code_multiview_embedding", False)
    monkeypatch.setattr(settings, "use_per_window_chunk_storage", False, raising=False)
    # SHA provenance is best-effort; don't let it reach for GitLab in a unit test.
    monkeypatch.setattr(ci, "_resolve_branch_head_sha", lambda repo, branch: None)

    return {"db": _FakeDb(), "files": files}


class TestOnProgressPlumbing:

    def test_milestones_reported(self, java_ingest_deps):
        stages: list[str] = []
        result = ci.ingest_repo(
            db=java_ingest_deps["db"], repo_id="r1",
            repo="grp/proj", branch="main",
            on_progress=lambda stage, pct=None: stages.append(stage),
        )
        assert result["chunks_stored"] == 1
        assert any(s.startswith("Fetched 1") for s in stages), stages
        assert any(s.startswith("Chunked") for s in stages), stages
        assert any(s.startswith("Embedding + storing") for s in stages), stages

    def test_raising_callback_does_not_abort_ingest(self, java_ingest_deps):
        def boom(stage, pct=None):
            raise RuntimeError("progress sink down")

        result = ci.ingest_repo(
            db=java_ingest_deps["db"], repo_id="r1",
            repo="grp/proj", branch="main",
            on_progress=boom,
        )
        assert result["chunks_stored"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# T5 — empty fetch must not wipe the existing index
# ──────────────────────────────────────────────────────────────────────────────

class TestEmptyFetchNoWipe:

    def test_ingest_repo_preserves_index(self, monkeypatch):
        monkeypatch.setattr(ci, "_fetch_java_files_from_gitlab",
                            lambda repo, branch: [])
        db = MagicMock()
        result = ci.ingest_repo(db=db, repo_id="r1", repo="grp/proj", branch="main")
        assert result == {"files_fetched": 0, "chunks_stored": 0,
                          "index_preserved": True}
        db.query.assert_not_called()
        db.delete.assert_not_called()
        db.commit.assert_not_called()

    def test_ingest_polyglot_repo_preserves_index(self, monkeypatch):
        monkeypatch.setattr(ci, "_fetch_files_by_extensions",
                            lambda repo, branch, exts: [])
        db = MagicMock()
        result = ci.ingest_polyglot_repo(
            db=db, repo_id="r1", repo="grp/proj", branch="main",
            languages=["java"],
        )
        assert result["index_preserved"] is True
        assert result["chunks_stored"] == 0
        assert result["by_language"] == {}
        db.query.assert_not_called()
        db.delete.assert_not_called()
        db.commit.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# T3 — duplicate-trigger guard (HTTP 409)
# ──────────────────────────────────────────────────────────────────────────────

class TestDuplicateTriggerGuard:

    def _repo_db(self):
        repo = MagicMock(gitlab_repo="grp/proj", gitlab_branch="main",
                         gitlab_url=None, label="Proj")

        class _Db:
            def get(self, model, rid):
                return repo

        return _Db()

    def test_second_trigger_rejected_with_409(self, monkeypatch):
        monkeypatch.setattr(
            api.job_registry, "get_active_jobs",
            lambda db, **kw: [{"subtype": "repo-1", "id": "job-9",
                               "current_stage": "Embedding + storing chunks 10/400"}],
        )
        created = {"n": 0}
        monkeypatch.setattr(api.job_registry, "create_job",
                            lambda *a, **kw: created.update(n=created["n"] + 1) or "job-1")

        with pytest.raises(HTTPException) as exc:
            api.index_repo("repo-1", BackgroundTasks(), self._repo_db(), MagicMock())
        assert exc.value.status_code == 409
        assert "job-9" in str(exc.value.detail)
        assert created["n"] == 0, "create_job must not run after the 409"

    def test_other_repos_job_does_not_block(self, monkeypatch):
        monkeypatch.setattr(
            api.job_registry, "get_active_jobs",
            lambda db, **kw: [{"subtype": "other-repo", "id": "job-9",
                               "current_stage": "Cloning"}],
        )
        monkeypatch.setattr(api.job_registry, "create_job", lambda *a, **kw: "job-1")

        result = api.index_repo("repo-1", BackgroundTasks(), self._repo_db(), MagicMock())
        assert result["job_id"] == "job-1"

    def test_guard_error_fails_open(self, monkeypatch):
        def boom(db, **kw):
            raise RuntimeError("fake DB without .query")

        monkeypatch.setattr(api.job_registry, "get_active_jobs", boom)
        monkeypatch.setattr(api.job_registry, "create_job", lambda *a, **kw: "job-1")

        result = api.index_repo("repo-1", BackgroundTasks(), self._repo_db(), MagicMock())
        assert result["job_id"] == "job-1", "a guard failure must not block indexing"


# ──────────────────────────────────────────────────────────────────────────────
# T6 — incremental fast path: deletions from the Compare API only
# ──────────────────────────────────────────────────────────────────────────────

class TestFastPathDeletions:

    def test_unchanged_files_are_not_deleted(self, monkeypatch):
        modified_file = {"path": "pkg/a.py", "content": "print(2)\n",
                         "language": "python"}
        # Full prior state: 100 files the compare-changed subset does NOT
        # contain — the old `diff["deleted"] | deleted_from_compare` union
        # classified every one of them as deleted and wiped the index.
        prior_state = {f"pkg/f{i}.py": f"hash{i}" for i in range(100)}
        prior_state["pkg/a.py"] = "stale-hash"       # differs → modified

        monkeypatch.setattr(ci, "_load_repo_state_sha", lambda db, rid: "aaa")
        monkeypatch.setattr(ci, "_resolve_branch_head_sha", lambda repo, branch: "bbb")
        monkeypatch.setattr(ci, "_fetch_changed_paths_via_compare",
                            lambda repo, a, b, exts: ([modified_file], []))
        monkeypatch.setattr(ci, "_load_prior_file_state", lambda db, rid: prior_state)

        deletion_buckets: list[list[str]] = []

        def record_delete(db, rid, source_files, *, categories=None):
            deletion_buckets.append(list(source_files))
            return 0

        monkeypatch.setattr(ci, "_delete_chunks_for_files", record_delete)
        monkeypatch.setattr(ci, "_delete_file_state_entries", lambda db, rid, paths: None)
        monkeypatch.setattr(ci, "_persist_file_state", lambda db, rid, files: None)
        monkeypatch.setattr(ci, "_persist_repo_state_sha",
                            lambda db, rid, sha, branch=None: None)

        def fake_chunk_source_file(path, content, language, *, fallback=None):
            return [{
                "path": path, "class_name": None, "method_name": None,
                "content": content, "chunk_index": 0, "language": language,
            }]

        monkeypatch.setattr(ci.code_chunker_ts, "chunk_source_file", fake_chunk_source_file)
        monkeypatch.setattr(ci, "embed_chunks_with_cache",
                            lambda db, batch: [[0.0] * 768 for _ in batch])
        for flag in ("use_symbol_graph_extractor", "use_python_lsp",
                     "use_typescript_lsp", "use_java_lsp",
                     "use_code_multiview_embedding"):
            monkeypatch.setattr(settings, flag, False)
        monkeypatch.setattr(settings, "use_per_window_chunk_storage", False,
                            raising=False)

        result = ci.ingest_polyglot_repo_incremental(
            db=_FakeDb(), repo_id="r1", repo="grp/proj", branch="main",
            languages=["python"],
        )

        assert result["files_deleted"] == 0
        assert result["files_modified"] == 1
        # First _delete_chunks_for_files call is the deletion bucket — it must
        # be empty; the second is the re-ingest clear for the modified file.
        assert deletion_buckets[0] == []
        assert deletion_buckets[1] == ["pkg/a.py"]
