# Task Sheet — Run token budget: cache tokens counted at full weight kills healthy runs

**Date raised:** 2026-10-01
**Status:** APPLIED 2026-10-07 — T1 (`get_run_token_breakdown` + cost-weighted `get_cumulative_run_tokens` in `core/observability.py`), T2 (weighted total + in/out/cache_read/cache_write breakdown in the `token_budget_exceeded`/`token_budget_warning` payloads and logs), T3 (in-loop guard moved BEFORE the LLM call in `agentic_runtime.run_agent_loop`, plus a drive-entry budget preflight in `agentic_orchestrator.drive_run`), T4 (cap kept at 4M, documented as cost-weighted at the `agentic_token_budget_hard_cap` config site). Pending deploy + worker restart; the interim raised-cap ops workaround can be reverted once deployed.
**Severity:** Critical on the AiNxt deployment — any analysis run needing more than ~60 LLM calls hits `TOKEN BUDGET EXCEEDED` and stops at the clarification/analysis stage; once over the cap the run can **never** be resumed without an operator raising the cap (each resume attempt adds ~21k and dies again).

**Related sheets:** [llm-cache-marker-gateway-limit.md](llm-cache-marker-gateway-limit.md) (the `Found 5` resume storm that wasted a day of run `a5f489f8`), [agentic-convergence-fixes.md](agentic-convergence-fixes.md) (compaction summary truncation §B, over-budget auto-resume re-billing §C).

---

## Problem (confirmed)

Agentic runs die in `agent_loop[analysis]` with:

> `ERROR [app.agentic] agent_loop[analysis] run=… TOKEN BUDGET EXCEEDED: 4014052 >= cap 4000000 — stopping (resumable with a raised budget)`

The user-visible symptom is the clarification stage failing "token utilisation exhausted" — but clarification is only where the guard *fires*; the budget was consumed across the run's whole lifetime.

### Ledger evidence (run `a5f489f8`, queried 2026-10-01)

```
hr (IST)          section           calls  inp  outp    cread     cwrite
2026-09-29 19:00  analysis             20   40   7920  1016386   231091
2026-09-29 19:00  analysis:compact      2    4   4000    11280   126251
2026-09-30 10:00  analysis             35   70  11807  2297258   229603
2026-09-30 10:00  analysis:compact      2    4   4000    11280    63058
2026-09-30 11:00  analysis (retries)    6   12   1490    83312    41656
2026-09-30 12:00  analysis (retries)    3    6    803    41656    20828
TOTAL                                  68 ~136  ~30k    ~3.46M    ~712k   = 4.20M
```

- **99.3% of the "4M tokens" are cache tokens.** Real work: 57 analysis calls, ~30k output tokens, ~65k context — a normal, healthy session. Prompt caching worked perfectly; the guard counted the *same cached ~65k prefix at full weight on every one of 68 calls* (68 × 65k ≈ 4.3M).
- **Cost-weighted** (cache read ≈ 10% price, write ≈ 125%): ~1.27M token-equivalents — a third of the cap. The guard killed a run whose true spend was fine.
- `input_tokens = exactly 2 per call`: the AiNxt gateway reports only the uncached tail as input.

### Why it "used to work" (the regression)

The pre-upgrade AiNxt gateway stripped usage / hardcoded cache counters to 0 (noted in `agentic_runtime.py` ~line 954), and `_persist_usage_row` skips rows with no token data — so the ledger recorded almost nothing and the 4M cap **never actually fired**. The gateway upgrade rolled out 2026-09-29/30 (same window as the `Found 5` enforcement and the forced `ainxt-cli` 3.0 upgrade) started reporting honest cache counters. Same workload: old ledger ≈ 30k, new ledger ≈ 4.2M — **~140× inflation in measured spend**. The cap was calibrated against the blind meter.

### Secondary defect: over-cap runs are unresumable by design

`get_cumulative_run_tokens` is a durable lifetime SUM per run_id. Once over cap, every retry makes one call (~21k, persisted), then the guard reads the ledger and stops. Run `a5f489f8`: 10 failed retries 2026-09-30 10:59 → 2026-10-01 15:06, 4.01M → 4.13M, pure waste. (Auto-resume side covered in agentic-convergence-fixes §C.)

---

## Tasks

### T1 — Cost-weight the budget sum ✅ (2026-10-07)

`backend/app/core/observability.py`, `get_cumulative_run_tokens` (~line 383). Replace the flat
`input + output + cache_read + cache_write` SUM with a cost-weighted total matching real billing:

```python
totals = db.execute(
    select(
        func.coalesce(func.sum(LlmUsageRecord.input_tokens), 0),
        func.coalesce(func.sum(LlmUsageRecord.output_tokens), 0),
        func.coalesce(func.sum(LlmUsageRecord.cache_read_tokens), 0),
        func.coalesce(func.sum(LlmUsageRecord.cache_write_tokens), 0),
    ).where(LlmUsageRecord.run_id == run_id)
).one()
inp, out, cread, cwrite = (int(x or 0) for x in totals)
return int(inp + out + 0.1 * cread + 1.25 * cwrite)
```

Run `a5f489f8` under this formula: ~1.27M / 4M — finishes with 3× headroom.

**Alternative considered:** cap on `sum(cost_usd)` (column already persisted) — truest to intent, but needs a new `$`-denominated setting and cost rows are None when pricing is unknown; the weighted-token form keeps the existing cap/env var meaningful. Decide at review.

### T2 — Make the exceeded-stop message reflect weighting ✅ (2026-10-07)

`backend/app/agents/agentic_runtime.py` (~lines 991-1021): log/emit the weighted total plus the raw component breakdown (`inp/out/cread/cwrite`) in `token_budget_exceeded` / `token_budget_warning` payloads, so the next person debugging sees *what kind* of tokens filled the budget without a DB query.

### T3 — Don't re-bill doomed resumes ✅ (2026-10-07)

Check the budget **before** the first LLM call of a (re)entered loop, not only after each turn — a run already over cap then stops at zero marginal cost instead of ~21k per retry. (Complements agentic-convergence-fixes §C which stops the auto-resume trigger itself.)

### T4 — Recalibrate the cap after T1 ✅ (2026-10-07 — kept 4M, documented as cost-weighted at the config site)

With honest accounting, revisit `agentic_token_budget_hard_cap` (config.py:726, default 4M). Under cost-weighting 4M ≈ a genuinely large run; keep 4M or raise per deployment appetite. Document in `.env.process` that the cap is now cost-weighted tokens.

---

## Ops workaround (active until T1 lands) — caching stays ON

In `.env.process`, raise the cap to absorb the ~3–4× cache inflation, then restart the agentic worker:

```
AGENTIC_TOKEN_BUDGET_HARD_CAP=16000000
```

```
pm2 restart atom-agentic atom-worker --update-env
```

This both unsticks the over-cap runs (they are "resumable with a raised budget") and gives new runs realistic headroom. Alternative blunt switch: `AGENTIC_TOKEN_BUDGET_ENFORCE=false` (iteration + continuation caps still bound runaway runs), but prefer the raised cap — it keeps a guard in place.

`PROMPT_CACHE_ENABLED=false` was considered and REJECTED (operator decision 2026-10-01: keep prompt caching on). Instead, Change 1 of [llm-cache-marker-gateway-limit.md](llm-cache-marker-gateway-limit.md) (keep=2 → keep=1) was applied the same day — it prevents the `Found 5` resume storms while retaining the system/tools/tail cache breakpoints.

## Verification

1. `SELECT` the component sums for a fresh run; confirm weighted total matches the guard's logged number.
2. Re-drive a previously stuck run with the raised cap: it proceeds past clarification instead of dying on iteration 1.
3. A deliberately long run (>60 iterations, warm cache) stays far below cap under T1 weighting.
4. Grep worker logs: no `TOKEN BUDGET EXCEEDED` on runs whose cost-weighted spend is under cap.
