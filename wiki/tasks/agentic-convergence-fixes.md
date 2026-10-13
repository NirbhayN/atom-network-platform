# Task Sheet — Agentic runs: indexing killed by XML comments + compaction summary truncation

**Date raised:** 2026-09-30
**Status:** APPLIED 2026-10-07 — T1 (`jaxb_mapper` non-element guard, both QName sites), T2 (`agentic_compact_summary_max_tokens` → 6000 default in config.py), T3 (drive-entry budget preflight in `agentic_orchestrator.drive_run` — see also token-budget sheet T3: the in-loop guard now runs BEFORE the LLM call), T4 (`process_executor.run_sync` loop-safe via private-thread fallback). Pending deploy + worker restart.
**Severity:** High — together these two defects can stop an analysis run from ever converging: run `a5f489f8` burned its full 4M token budget; run `60734b93` shows the same pattern live (compaction every ~60s, every summary truncated).

---

## Problem (confirmed)

### A. Repo artifact indexing dies on any XML comment in a `.xjb` file

> `WARNING [app.agentic] repo artifact indexing skipped for 259184ba-…: Invalid input tag of type <class '_cython_3_2_4.cython_function_or_method'>`

- `jaxb_mapper.py:108-115` iterates `root.iter()` and calls `etree.QName(b)` on **every** node. XML comment / processing-instruction nodes have a *callable* `.tag` (lxml's `etree.Comment` function, the "cython_function_or_method" in the message); `QName()` rejects it and raises.
- One comment in one `.xjb` binding file aborts the **whole** try-block in `agentic_orchestrator.py:395-403` — XSD graph, JAXB links, and module/flow context are all skipped for the repo, on every run (deterministic).
- Downstream effect (the orchestrator's own event says it): *"reuse/schema discovery may be weaker"* — the agent starts blind, rediscovers repo structure via tool calls each run → far more iterations and tokens.
- The identical trap is already guarded in `module_context_generator.py:57-63` with an explanatory comment; `jaxb_mapper` never received the same guard.

### B. Compaction progress summary still truncated (now at 4000)

> `WARNING [app.core.llm] call_messages_api_tools truncated by max_tokens=4000 model=claude-sonnet-5 tool_uses=0`

- Each truncation immediately follows a `preflight: … over policy — compacting before the call` line and has `tool_uses=0` → these are the **compaction summary** calls (`agentic_runtime.py:356/373`, budget = `settings.agentic_compact_summary_max_tokens`, `config.py:767`).
- Cap was already raised 2000 → 4000; the summary **still consumes 100% of the budget every time** and is cut off mid-write.
- A truncated summary = the agent loses part of its "goal / files / edits / what's left" state after each eviction → re-explores work it already did → context refills → compacts again. Run `60734b93` compacted at 12:19, 12:20, 12:21, 12:22, 12:25 — input stayed over policy at 260k→274k→277k between compactions (thrash) before finally dropping to 106k.
- Run `a5f489f8` (2026-09-30): same fingerprint, ended in `TOKEN BUDGET EXCEEDED: 4.0M+`.

### C. Over-budget runs are auto-resumed and re-billed

- After `a5f489f8` first exceeded the 4M cap (11:29), six more `TOKEN BUDGET EXCEEDED` stops followed (11:30 → 11:45), each ~21k tokens higher (4,014,052 → 4,140,522). Something auto-resumes a run that is already over budget; each resume pays for one more LLM call before dying. ~126k tokens of pure waste in 15 min.

### D. (Minor) `jdk_discovery` coroutine never awaited

> `RuntimeWarning: coroutine 'ProcessExecutor.run' was never awaited` at `jdk_discovery.py:113`

- `run_sync()` (`process_executor.py:301`) uses `asyncio.run()`, which refuses inside an already-running event loop; some async code path calls JDK discovery. The prepared coroutine is discarded (the warning) and the blanket `except Exception: continue` at `jdk_discovery.py:112` hides the real error.
- Impact low: the `update-alternatives` lookup silently yields nothing; the directory-scan fallback still finds JDKs.

---

## Tasks

### T1 — `jaxb_mapper`: skip non-element nodes before `QName()` ✅ (2026-10-07)

`backend/app/agents/jaxb_mapper.py` (~line 108): guard both `QName()` call sites the same way `module_context_generator._localname` does:

```python
for b in root.iter():
    if not isinstance(b.tag, str):        # comment / PI nodes — callable .tag, QName() rejects
        continue
    if etree.QName(b).localname != "bindings":
        ...
```

(and the inner loop at ~line 115 for `c`). Highest-leverage fix: restores artifact indexing + the reuse-first gate for the affected repo.

### T2 — raise `agentic_compact_summary_max_tokens` 4000 → 6000 ✅ (2026-10-07)

`backend/app/core/config.py:767`, or via env `AGENTIC_COMPACT_SUMMARY_MAX_TOKENS=6000` (settings read `.env`, `extra="ignore"`).
Watch for the `consumed the FULL budget (6000/6000)` warning after; if it still fires routinely, go to 8000. Do **not** go far beyond ~15% of the compaction trigger — the retained floor (system + summary + `agentic_compact_keep_recent_turns=6`) must stay well under the trigger or compaction thrashes (see warning at `config.py:747-751`; thrash observed live in run `60734b93`).

### T3 — stop auto-resume of runs already over token budget ✅ (2026-10-07)

Find the resume path (pause/auto-resume machinery in `agentic_orchestrator.py`) and add a pre-resume check: if `tokens_used >= cap`, do not resume — mark the run paused-for-budget and require an explicit operator raise. Saves one full LLM call per futile resume.

### T4 — `jdk_discovery`: make the executor call loop-safe ✅ (2026-10-07)

Either detect a running loop in `run_sync()` and fall back to `asyncio.to_thread`-style execution, or change the async call sites to `await default_executor.run(...)`. Low priority; currently only degrades JDK discovery to the fallback scan.

---

## Verification

- After T1: trigger indexing for repo `259184ba-…`; expect `repo_indexed` event with non-zero XSD/JAXB counts instead of `repo artifact indexing skipped`.
- After T2: run a long analysis; the `output consumed the FULL budget` warning for `tool_uses=0` calls should stop appearing (or become rare).
- After T3: force a run past its cap; expect exactly one `TOKEN BUDGET EXCEEDED` and no further token growth.
