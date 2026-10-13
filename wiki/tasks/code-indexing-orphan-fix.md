# Task Sheet — Code Indexing: orphan-sweeper kills long ingests

**Date raised:** 2026-09-29
**Status:** APPLIED 2026-10-07 — T1–T7 all landed (`_JobHeartbeat` + wiring + 409 guard in `api/code_indexing.py`; `on_progress` plumbing, fetch-first/no-wipe reorder, Compare-API-only deletions in `rag/code_ingestion.py`; tests in `tests/rag/test_code_indexing_resilience.py`, all green incl. the three neighbouring suites). Pending deploy; the interim ops workaround (stop atom-beat / manual row bumps) can be retired once deployed.
**Severity:** High — large repos can never be indexed; retries fail deterministically and can wipe a previously-good index.

---

## Problem (confirmed)

Indexing a large repo runs 1.5–2 h and then the repo shows **0 files / 0 chunks**. Confirmed root cause from `agent_jobs`:

> `error_message = "Orphaned — no updates for >90 minutes; swept by orphan-job task."`

Mechanism:

1. `_run_index_job` (`backend/app/api/code_indexing.py`) writes the job row **only at start and end**. During the multi-hour fetch → chunk → embed pipeline, `agent_jobs.updated_at` is frozen.
2. Celery beat runs `jobs.sweep_orphan_jobs` every 30 min (`backend/app/services/celery_tasks.py`, beat entry `agent-jobs-orphan-sweeper`); it fails any RUNNING job with `updated_at` older than 90 min. It has **no module filter** — code-indexing jobs are in scope.
3. Any ingest longer than 90 min is therefore reaped mid-flight (between the 90- and 120-minute mark). The repo row's `files_count`/`chunks_count` are only written on success, so a never-successful repo displays 0/0.
4. Precedent in-code: the beat-schedule comment records the threshold was already raised 30→90 min after the sweeper killed healthy code-indexing jobs sitting silent 40+ min in LLM summarization. Raising thresholds loses this race at some repo size; the missing piece is a heartbeat.

Aggravating defects found during the same investigation:

- **Empty fetch wipes the index**: `ingest_repo` / `ingest_polyglot_repo` delete all existing chunks *before* fetching; a failed/empty fetch (wrong branch, token scope, rate-limited per-file fallback) then commits the wipe and reports "Indexed 0 files, 0 chunks" as success.
- **No duplicate-trigger guard**: `POST /repos/{id}/index*` always creates a new job; concurrent runs delete each other's chunks mid-flight (operator retries make this likely).
- **Incremental fast-path wipe bug** (flagged as TODO in code, unfixed): on the first changed-commit run, the Compare-API fast path computes `deleted` = every *unchanged* file and erases the whole index.
- **Poisoned session on failure**: the `except` path calls `fail_job` on the same session that may hold a failed transaction; if that write also fails, the job stays RUNNING until swept.

## Interim operational workaround (until the tasks below land)

- `pm2 stop atom-beat` for the duration of the run (re-start after), **or** bump the row manually every ≤60 min:
  `UPDATE agent_jobs SET updated_at = now() WHERE module='code_indexing' AND status IN ('running','pending');`
- Trigger the run once; never restart `atom-api` mid-run; avoid the incremental endpoint; optionally index one language per run.

---

## Tasks

### T1 — `_JobHeartbeat`: keep long index jobs visibly alive ✅ (2026-10-07)

**File:** `backend/app/api/code_indexing.py`
**Why:** `updated_at` must move while the ingest is genuinely alive so `sweep_orphan_jobs` only reaps dead jobs. Every write uses its **own short-lived session** — the ingest session holds the uncommitted delete+insert transaction, and `update_job` commits whatever session it is handed.

Add `import os`, `import threading` to the module imports (and drop the inline `import threading` inside `_run_index_job`). Then add:

```python
# ── Job heartbeat ────────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL_S = float(os.getenv("CODE_INDEX_HEARTBEAT_SECONDS", "300"))


class _JobHeartbeat:
    """Keeps a long-running index job visibly alive for the R-9 orphan sweeper.

    The sweeper (`jobs.sweep_orphan_jobs`, Celery beat every 30 min) fails any
    RUNNING job whose `updated_at` is older than 90 minutes. `_run_index_job`
    used to write the job row only at the start and the end, so any repo whose
    ingest exceeded 90 minutes was reaped mid-flight — the same incident the
    earlier 30→90 min threshold raise (see celery_tasks.py beat_schedule) tried
    to outrun by widening the window. A heartbeat removes the race at any repo
    size: `updated_at` moves every HEARTBEAT_INTERVAL_S while the ingest is
    alive, and stops when the process dies, so the sweeper's contract (reap
    dead jobs, spare live ones) actually holds.

    Two write paths share one clock:
      - the background beat thread fires every `interval_s` with the last
        known stage (covers silent phases: archive download, LLM
        summarization, LSP resolution);
      - `progress(stage, pct)` — passed as `on_progress` into the ingest —
        writes immediately AND becomes the stage the next beat repeats.

    Every write opens its OWN short-lived session. The ingest session holds
    the uncommitted delete+insert transaction, and `update_job` commits
    whatever session it is handed — committing the ingest session mid-flight
    would tear its all-or-nothing semantics.

    `stop()` must be called BEFORE complete_job/fail_job so a late beat
    cannot overwrite the terminal stage text.
    """

    def __init__(self, job_id: str, *, interval_s: float = HEARTBEAT_INTERVAL_S,
                 touch_fn=None):
        self._job_id = job_id
        self._interval_s = interval_s
        self._touch_fn = touch_fn or self._touch_via_own_session
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._stage: str | None = None
        self._pct: int | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"idx-heartbeat-{job_id[:8]}", daemon=True,
        )

    # touch_fn injection point exists for tests; production always goes
    # through a fresh session.
    def _touch_via_own_session(self, stage: str | None, pct: int | None) -> None:
        hb_db = SessionLocal()
        try:
            job_registry.update_job(hb_db, self._job_id,
                                    current_stage=stage, progress_pct=pct)
        finally:
            hb_db.close()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def progress(self, stage: str, pct: int | None = None) -> None:
        """`on_progress` hook for the ingest — record + write through now."""
        with self._lock:
            self._stage = stage
            if pct is not None:
                self._pct = pct
            stage_now, pct_now = self._stage, self._pct
        if self._stop_evt.is_set():
            return
        self._safe_touch(stage_now, pct_now)

    def _safe_touch(self, stage: str | None, pct: int | None) -> None:
        try:
            self._touch_fn(stage, pct)
        except Exception as e:  # noqa: BLE001 — a broken heartbeat must never sink the ingest
            logger.debug("[heartbeat %s] touch failed: %s", self._job_id, e)

    def _run(self) -> None:
        while not self._stop_evt.wait(self._interval_s):
            with self._lock:
                stage_now, pct_now = self._stage, self._pct
            # update_job bumps updated_at even with stage=None/pct=None, so a
            # beat before the first progress() call still counts as liveness.
            self._safe_touch(stage_now, pct_now)
```

**Accept:** a job idling in any phase gets `updated_at` bumped every ≤5 min; a killed process stops beating; no beat lands after the terminal stage is written.

---

### T2 — Wire heartbeat + progress through `_run_index_job` ✅ (2026-10-07)

**File:** `backend/app/api/code_indexing.py` (`_run_index_job`)

Changes:
1. After the initial `"Cloning …"` `update_job`, create and start the heartbeat: `heartbeat = _JobHeartbeat(job_id); heartbeat.start()` (declare `heartbeat: _JobHeartbeat | None = None` before the `try`).
2. Pass `on_progress=heartbeat.progress` into all three calls: `ingest_repo`, `ingest_polyglot_repo`, `ingest_polyglot_repo_incremental` (requires T4).
3. Call `heartbeat.stop()` **before every terminal write**: before the unknown-mode `fail_job`, after `mc_thread.join(...)` before the success path, in the `except` branch, and defensively in `finally`.
4. In the `except` branch, call `worker_db.rollback()` before `fail_job` so the failure write runs on a clean session:

```python
    except Exception as exc:
        logger.exception("[index-job %s] failed", job_id)
        if heartbeat is not None:
            heartbeat.stop()
        try:
            # The worker session may hold a failed transaction — roll it back
            # so the job-row write below runs on a clean session.
            worker_db.rollback()
            job_registry.fail_job(
                worker_db, job_id,
                error=str(exc), final_stage="Indexing failed",
            )
        except Exception:
            pass
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        worker_db.close()
```

5. Empty-fetch outcome (pairs with T5): when `files_fetched == 0` and `mode != "polyglot-incremental"`, complete the job **without touching repo stats**:

```python
        # Terminal writes from here — no beat may land after them.
        heartbeat.stop()

        # Empty fetch (java/polyglot full modes): the ingest preserved the
        # existing index, so do NOT overwrite the repo stats with 0/0 — that
        # made a fetch failure look like a successful empty index. Surface it
        # on the job instead. (Incremental's 0-file case is a legit no-op.)
        if files_fetched == 0 and mode != "polyglot-incremental":
            job_registry.complete_job(
                worker_db, job_id,
                result={
                    "repo_id":         repo_id,
                    "mode":            mode,
                    "files_fetched":   0,
                    "chunks_stored":   0,
                    "index_preserved": True,
                    **extra,
                },
                final_stage=(
                    "Fetched 0 files — existing index left unchanged. "
                    "Check branch name, token access and selected languages."
                ),
            )
            logger.warning("[index-job %s] fetch returned 0 files — repo stats untouched", job_id)
            return
```

**Accept:** long ingests survive the sweeper; failures always land the job in FAILED (not stuck RUNNING); a 0-file run never zeroes `files_count`/`chunks_count`/`last_indexed_at`.

---

### T3 — Duplicate-trigger guard (HTTP 409) ✅ (2026-10-07)

**File:** `backend/app/api/code_indexing.py`
**Why:** no guard exists; a retry click starts a second ingest that deletes the first one's chunks mid-flight and races it on the repo stats row. Fail-open so a guard error can't block indexing (existing endpoint tests use fake DBs without `.query` — fail-open also keeps them green).

Add above `index_repo`:

```python
def _active_job_for_repo(db, repo_id: str) -> dict | None:
    """Duplicate-trigger guard: the most recent active code-indexing job for
    this repo, or None. Ingest runs are legitimately multi-hour (now kept
    alive by the heartbeat), and a second concurrent run would delete the
    first run's chunks mid-flight and race it on the repo stats row.

    Best-effort — a guard failure must not block indexing (a real DB outage
    surfaces in create_job immediately after anyway)."""
    try:
        active = job_registry.get_active_jobs(db, module="code_indexing")
    except Exception as e:  # noqa: BLE001
        logger.debug("active-job guard skipped: %s", e)
        return None
    return next((j for j in active if j.get("subtype") == repo_id), None)


def _reject_if_already_indexing(db, repo_id: str) -> None:
    dup = _active_job_for_repo(db, repo_id)
    if dup:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Indexing is already running for this repo "
                f"(job {dup.get('id')}, stage: {dup.get('current_stage') or 'starting'}). "
                "Wait for it to finish before starting another run."
            ),
        )
```

Call `_reject_if_already_indexing(db, repo_id)` immediately after the 404 check in **all three** endpoints: `index_repo`, `index_repo_polyglot`, `index_repo_polyglot_incremental`. The frontend already surfaces `detail` (`CodeIndexing.jsx` uses `e.response?.data?.detail`), so no UI change is needed.

**Accept:** second trigger while a job is active → 409 with the running job's id/stage; after the job reaches a terminal state (including swept), triggering works again.

---

### T4 — `on_progress` plumbing in the ingest pipeline ✅ (2026-10-07)

**File:** `backend/app/rag/code_ingestion.py`
**Why:** milestones double as the liveness signal (each write bumps `updated_at`) and give the admin page real progress. Progress must never break ingest.

1. Imports + helper near `BATCH_SIZE`:

```python
from collections.abc import Callable

# Coarse progress reporting for the multi-hour admin ingest. The REST index
# job passes a callback that writes (stage, pct) onto the agent_jobs row —
# each write bumps `updated_at`, which is what the R-9 orphan sweeper reads
# to tell a live ingest from a dead one. Milestones therefore double as the
# job's liveness signal, not just UI decoration.
ProgressFn = Callable[[str, "int | None"], None]


def _notify(on_progress: ProgressFn | None, stage: str, pct: int | None = None) -> None:
    """Report an ingest milestone to the caller. Must never raise — a broken
    progress sink cannot be allowed to sink the ingest itself."""
    if on_progress is None:
        return
    try:
        on_progress(stage, pct)
    except Exception as e:  # noqa: BLE001
        logger.debug("ingest on_progress callback failed: %s", e)
```

2. Add keyword-only `on_progress: ProgressFn | None = None` to `ingest_repo`, `ingest_polyglot_repo`, `ingest_polyglot_repo_incremental`.

3. Milestones (same shape in all three entrypoints):
   - after fetch: `_notify(on_progress, f"Fetched {n} files — clearing old index", 10)`
   - after chunking: `_notify(on_progress, f"Chunked {files} files → {chunks} chunks", 20)`
   - polyglot, if any LSP flag on: `_notify(on_progress, "Resolving cross-file references (LSP)", 21)`
   - if `settings.use_code_multiview_embedding`: `_notify(on_progress, f"Generating NL summaries for {n} chunks (multiview)", 22)` — the longest silent phase (~55 min for a 13k-symbol repo)
   - incremental, after the diff: `_notify(on_progress, f"Delta: {a} added, {m} modified, {d} deleted", 15)`
   - **per embed batch** (the dominant phase — set `total_chunks = len(all_chunks)` before the loop, then after each `db.flush()`):

```python
        db.flush()
        # Per-batch milestone — the embed phase dominates wall-time on big
        # repos, and these writes are what keeps the job's updated_at moving.
        _notify(
            on_progress,
            f"Embedding + storing chunks {total_stored}/{total_chunks}",
            25 + int(70 * total_stored / max(total_chunks, 1)),
        )
```

**Accept:** with a recording callback, an ingest over N batches produces ≥ N+2 notifications; an exception thrown by the callback does not abort the ingest.

---

### T5 — Empty fetch must not wipe the existing index ✅ (2026-10-07)

**File:** `backend/app/rag/code_ingestion.py` (`ingest_repo`, `ingest_polyglot_repo`)
**Why:** current order is *clear → fetch*; a failed/empty fetch commits the wipe (the flushed deletes ride along with `_run_index_job`'s stats commit) and a previously-good index becomes 0/0.

Reorder both functions to *fetch → verify non-empty → clear → ingest*:

- Move the "Remove existing chunks" block to **after** the fetch + empty check (keep the `settings.gitlab_url` override wrap around the fetch exactly as-is).
- Empty return gains a marker and a clarified log, e.g. for `ingest_repo`:

```python
    if not java_files:
        logger.warning("No Java files returned from GitLab for repo=%s branch=%s "
                       "— existing index left untouched", repo, branch)
        return {"files_fetched": 0, "chunks_stored": 0, "index_preserved": True}
```

  (polyglot: same, keeping `"by_language": {}` in the dict.)
- Comment to carry on the reorder (why-record):

```python
    # Fetch FIRST, clear the old index only once the fetch proves non-empty.
    # The previous order (clear → fetch) meant any failed/empty fetch — wrong
    # branch, token without repository access, rate-limited per-file fallback —
    # wiped a previously-good index and left the repo showing 0 files/0 chunks.
```

Pairs with the `_run_index_job` guard in T2 step 5 (repo stats untouched on 0 files).

**Accept:** with the fetcher stubbed to `[]`, neither function touches the DB (no query/delete/commit) and both return `index_preserved: True`.

---

### T6 — Incremental fast path: deletions from Compare API only ✅ (2026-10-07)

**File:** `backend/app/rag/code_ingestion.py` (`ingest_polyglot_repo_incremental`, fast-path branch)
**Why:** flagged-but-unfixed TODO in the code. In fast-path mode `files` is only the compare-changed subset, so `diff_against_state(...)["deleted"]` = every **unchanged** file in the repo; ORing it in wipes the entire index (chunks + file-state) on the first real changed-commit run.

Replace:

```python
            deleted = sorted(set(diff["deleted"]) | set(deleted_from_compare))
```

with:

```python
            # Deletions come from the Compare API ONLY. `files` here is just
            # the compare-changed subset, so diff_against_state's `deleted`
            # (prior_state keys absent from `files`) is every UNCHANGED file
            # in the repo — ORing that in wiped the entire index (chunks +
            # file-state) on the first real changed-commit run.
            deleted = sorted(deleted_from_compare)
```

and delete the superseded TODO block above it.

**Accept:** fast-path run with full prior state and a 1-file modified delta → `files_deleted == 0`, `_delete_chunks_for_files` receives `[]` for the deletion bucket (the test the original TODO prescribes).

---

### T7 — Tests ✅ (2026-10-07 — backend/tests/rag/test_code_indexing_resilience.py, 13 tests)

**File (new):** `backend/tests/rag/test_code_indexing_resilience.py`
Follow the house patterns from `test_code_ingestion_lsp_wiring.py` (stub fetcher/chunker/embeds + `_FakeDb`, patch `embed_texts` at **both** `ci.embed_texts` and `app.rag.embeddings.embed_texts`) and `test_code_ingestion_polyglot.py` (call endpoints directly with `_FakeDb` + `MagicMock` user).

1. **Heartbeat:** with `interval_s=0.05` and a recording `touch_fn` — ≥2 beats while running; count frozen after `stop()`; `progress()` after stop writes nothing; `progress("s1", 10)` writes immediately and the next beat repeats `("s1", 10)`.
2. **`on_progress` plumbing (`ingest_repo`):** stubbed single-file ingest → milestones include "Fetched 1", a "Chunked", and ≥1 "Embedding + storing"; a raising callback does not abort the ingest.
3. **Empty-fetch no-wipe:** fetcher → `[]` for both `ingest_repo` and `ingest_polyglot_repo` with `MagicMock` db → result has `index_preserved: True`; `db.query`, `db.delete`, `db.commit` never called.
4. **409 guard:** `get_active_jobs` → `[{"subtype": "repo-1", "id": "job-9", "current_stage": "Embedding…"}]` → `index_repo("repo-1", …)` raises `HTTPException(409)` and `create_job` is not called; non-matching subtype → job created normally; guard raising internally (fake DB without `.query`) → fail-open, job created.
5. **Fast-path deletions (T6):** monkeypatch `_load_repo_state_sha→"aaa"`, `_resolve_branch_head_sha→"bbb"`, `_fetch_changed_paths_via_compare→([1 modified file], [])`, `_load_prior_file_state→{100 files}`, record `_delete_chunks_for_files` → deletion bucket `[]`, `files_deleted == 0`, `files_modified == 1`; flags (`use_symbol_graph_extractor`, 3× LSP, multiview, per-window) monkeypatched off.

Run: `backend/venv/bin/python -m pytest tests/rag/test_code_indexing_resilience.py tests/rag/test_code_ingestion_polyglot.py tests/rag/test_code_ingestion_lsp_wiring.py tests/rag/test_incremental_ingest.py -q` from `backend/`.

**Accept:** new tests pass; the three existing suites stay green (the fail-open guard in T3 is what keeps the untouched endpoint tests passing).

---

## Deferred / follow-ups (out of scope for this sheet)

- **Batch-wise or staged commits** for the ingest — today one multi-hour transaction (vacuum/xmin pressure, all-at-end visibility). Needs a generation-tag + atomic-swap design to keep crash semantics; don't change casually.
- **Explicit timeout on the GitLab client** (`gitlab.Gitlab(...)` is created with no timeout; the archive call can hang indefinitely) and **streaming the archive to disk** instead of `io.BytesIO(raw)` (whole-repo tarball in RAM → OOM risk on big repos).
- **Sweeper telemetry:** log job module/subtype when sweeping so a reaped indexing job is obvious in `pm2 logs atom-worker`.

## Verification after the fix ships

1. Re-index the failing repo; watch `agent_jobs.updated_at` move every ≤5 min while stages advance ("Embedding + storing chunks N/M …").
2. Confirm the run outlives 2 h without the sweeper touching it; terminal stage `Indexed N files, M chunks`, repo card non-zero.
3. `SELECT count(*) FROM document_chunks WHERE metadata_->>'repo_id' = '<repo_id>';` matches the reported chunk count.
4. Double-trigger during a run → 409 in the UI banner.
