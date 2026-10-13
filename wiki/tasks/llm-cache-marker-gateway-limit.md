# Task Sheet — LLM prompt caching: reduce explicit breakpoints 4→3 (AiNxt gateway injects a 5th)

**Date raised:** 2026-09-29
**Status:** Change 1 APPLIED 2026-10-01 in `backend/app/core/llm.py` (keep=2 → keep=1, with explanatory comment). Companion hardening APPLIED 2026-10-07 (`_strip_stale_cache_markers` wired into `call_messages_api_tools`; `LLMPermanentError` fast-fail on embedded `invalid_request_error` in `_messages_api_create_with_retry`). Both pending deploy + worker restart. Kill-switch workaround NOT used (operator wants caching kept on).
**Severity:** Critical on the AiNxt deployment — every agentic LLM call 400s, runs loop through pause/auto-resume forever. No impact on Claude-direct deployments.

---

## Problem (confirmed)

Agentic runs on the NPCI/AiNxt deployment fail every LLM call with:

> `Error code: 502 - {'detail': "Error code: 400 - … 'invalid_request_error' … 'A maximum of 4 blocks with cache_control may be provided. Found 5.' … 'request_id': 'req_011CfXjaPcahFvKE8xdMZqyp'}"`

The app classifies the 502 as transient (`_is_transient`, `agentic_orchestrator.py`) and retries 6× then pauses/auto-resumes up to 20× — futile, since the inner 400 is deterministic.

## Root cause (proven — not our code)

1. Our tool loop (`call_messages_api_tools`, `backend/app/core/llm.py`) sends **exactly 4** explicit cache breakpoints: 2 system (after `_trim_system_cache_breakpoints(…, keep=2)`) + 1 tools + 1 rolling message tail. This is Anthropic's documented maximum. All other call sites send ≤3 (audited 2026-09-29).
2. The AiNxt gateway **unconditionally injects a top-level `cache_control` marker** into every outbound request (`core/claude_cache_egress.py:101` per static trace of the gateway repo). Top-level `cache_control` is Anthropic's documented *automatic caching* feature and **consumes one of the 4 breakpoint slots**. Anthropic docs name our exact failure: *"If 4 explicit block-level breakpoints already exist, the API returns a 400 error (no slots left for automatic caching)."*
3. Proven empirically: a minimal 4-marker request sent through the app's own production SDK client via the gateway → "Found 5" (request_id above). Same shape direct to Anthropic → succeeds.
4. Second gateway defect: the upstream 400 is rewritten to 502 (`routers/message_compact/stream.py` drops the status in its SSE re-encoder, then hardcodes `HTTPException(502, …)`), which is what tricks our retry classifier.

## Change 1 — the one-character fix (this task)

`backend/app/core/llm.py`, in `call_messages_api_tools`:

```python
# before
messages_api_system = _trim_system_cache_breakpoints(messages_api_system, keep=2)
# after
messages_api_system = _trim_system_cache_breakpoints(messages_api_system, keep=1)
```

New layout: 1 system + 1 tools + 1 tail = **3 explicit + 1 gateway-injected automatic = 4** ✅.

Why the system breakpoint is the one to drop: Anthropic caches the entire prefix up to a breakpoint, so the last system breakpoint still covers all system segments — near-zero cache loss. Tools and rolling-tail markers (highest value) stay. Accuracy-neutral: markers change what is cached, never what the model reads.

**Revert condition:** restore `keep=2` once AiNxt deploys their fix (count client markers before injecting; preserve upstream 4xx). Track their ticket — do not let this accommodation become permanent.

## Companion hardening (optional, drafted in-session 2026-09-29, reverted pending approval)

- `_strip_stale_cache_markers(messages)` in `call_messages_api_tools`: copy-on-write removal of `cache_control` embedded in incoming messages, so a replayed/persisted transcript can never spend breakpoint budget the budget math doesn't count. Cheap; makes resume/replay bulletproof against transcripts written by any earlier build.
- `LLMPermanentError` + fast-fail in `_messages_api_create_with_retry`: when a nominally-transient exception embeds `invalid_request_error`, raise immediately instead of 6 retries; type name deliberately matches none of `_is_transient`'s keywords, so the run fails with the real error instead of 20 auto-resume cycles.

## Ops workaround (active until Change 1 lands)

`PROMPT_CACHE_ENABLED=false` in the backend `.env` + worker restart. With 0 explicit markers, the gateway's automatic marker still provides rolling prefix caching, so cost impact is modest. Remove the line when Change 1 (or the AiNxt fix) is deployed.

## Verification

1. Run an agentic job on the AiNxt deployment: no `Found 5`, no `TRANSIENT_RETRY` storms in worker logs.
2. Paused runs resume past `analyzing` / `xsd_discovery`.
3. Grep worker logs for `cache_control may be provided` → no new hits.

## External dependency — AiNxt ticket (filed separately)

Full investigation report delivered to the AiNxt team names both defects with file:line and minimal diffs:
- `core/claude_cache_egress.py`: skip injection when client already carries 4 markers.
- `routers/message_compact/stream.py`: propagate upstream status; forward 4xx as 4xx (reserve 502 for genuine connectivity failures).
