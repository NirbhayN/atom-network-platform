# Tasks

Fixes from the `ainxt` circuit-breaker outage investigation (2026-10-22): Claude models
disabled on the gateway caused 400s from aux agents (taxonomy, query_understanding) still
configured with `claude-haiku-4-5-20251001`, which kept the per-provider circuit breaker
open and blocked GPT models routed through the same `ainxt` provider.

## Code fixes

- [x] **Don't count non-transient 4xx client errors toward the circuit breaker.**
  Done — `counts_toward_breaker()` in [backend/app/core/resilience.py](backend/app/core/resilience.py)
  classifies before `_on_failure` records anything. Transport failures (no HTTP status),
  408/425/429 and 5xx count; every other 4xx fails the call without touching the circuit.
  `asyncio.CancelledError` is also excluded — `guarded_call` catches `BaseException`, so
  cancelling a run used to ding the breaker on its way out.

- [x] **Expose an admin endpoint for `reset_circuit()`.**
  Done — [backend/app/api/admin_llm_circuit.py](backend/app/api/admin_llm_circuit.py):
  `GET /api/admin/llm/circuit` (state per provider) and
  `POST /api/admin/llm/circuit/{provider}/reset`, both `AdminUser`-gated. Unknown
  provider names 404 rather than lazily creating phantom state. Responses name the
  host/pid, because breaker state is per process.

- [x] **Bonus, found while testing the above: `reset_circuit()` self-deadlocked.**
  It did `with _STATE_LOCK: st = _get_state(...)`, and `_get_state` takes the same
  non-reentrant `threading.Lock`. The first call would hang forever holding the lock
  every LLM call needs — i.e. the recovery path was worse than the outage. Never
  noticed because nothing invoked it. Now fetches state before locking, like every
  other mutator in the module.

- [ ] **Add `gpt-6-luna` to `_MODEL_CONTEXT_WINDOW` and `_MODEL_MAX_OUTPUT`** in
  [backend/app/core/tokens.py](backend/app/core/tokens.py) so context budgeting stops
  falling back to the 128K provider default with a warning on every call.
  BLOCKED on the real numbers: that table's values are read from a Models API, never
  inferred from a sibling model, and guessing high silently truncates prompts. Get
  `max_input_tokens` / max output from `GET {AINXT_BASE_URL}/v1/models/gpt-6-luna`
  (or the ainxt team), then add both entries.

## Deployment config (operational, per environment)

- [ ] Replace every setting still holding a Claude model ID with a model the gateway
  serves: `routing_model_routing`, `routing_model_utility`; also audit
  `code_summarizer_model`, `adversarial_reviewer_model`, `eval_critic_default_model`,
  `deep_research_model`.

- [ ] Verify `ainxt_compat_mode=openai` and `ainxt_model=<gpt id>` — in `anthropic`
  compat mode the default model is `claude-sonnet-5`, which the gateway has disabled.

- [ ] Restart the backend after the config change — circuit-breaker state is
  process-local, so a restart clears the open circuit immediately.
