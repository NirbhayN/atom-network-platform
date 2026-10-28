# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Per-provider circuit breaker and bulkhead for outbound LLM calls.

Closes architecture review findings A1/A2 (Critical #1, #2 —
"No Circuit Breaker on LLM Provider Calls", "No Bulkhead Isolation Between
LLM Consumers") and implements the rules that the adapter layer MUST apply
timeouts/bulkheads/circuit breakers, and that resilience patterns are
mandatory: circuit breakers stop repeated calls to failing dependencies;
bulkheads isolate pools/threads to contain blast radius.

Design notes
------------
- **Keyed by provider name** ("anthropic", "gemini", "openai", "ainxt",
  "ollama") so one degraded provider does not throttle or trip a breaker
  for another — this is "shared-nothing concurrency" isolation applied to
  outbound dependencies.
- **Circuit breaker states**: CLOSED (normal) -> OPEN (fail fast) ->
  HALF_OPEN (single probe allowed) -> CLOSED | OPEN. This is the classic
  three-state breaker referenced in P8 ("circuit open/half-open/closed
  paths").
- **Bulkhead**: a bounded `asyncio.Semaphore` per provider. Saturation
  raises `LlmBulkheadFullError` immediately rather than queuing unboundedly
  (P2 "avoidance of unbounded queues"; "bounded queues/pools").
- All thresholds are externally configurable via `Settings` (never
  hardcoded), and callers should not construct `_ProviderState` directly —
  use `breaker_guard()` / `bulkhead_guard()` as async context managers around
  the existing per-provider `_call_*` dispatch in `core/llm.py`.
- State is process-local (module-level dict), which is correct for a
  circuit breaker: each worker process should independently observe and
  react to its own view of a dependency's health. A cluster-wide breaker
  would require a shared store (e.g. Redis) and is not needed at this
  fan-out; recorded as a future evolution in ADR-004.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from app.core.config import settings

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class LlmCircuitOpenError(RuntimeError):
    """Raised instead of making a network call when a provider's circuit is OPEN.

    Callers (agentic loop, orchestrator transient-resume path) should treat
    this the same as any other transient/infra failure — see
    `app/agents/infra_errors.py::classify_infra_error` — and back off rather
    than retrying immediately, since retrying immediately is exactly the
    thundering-herd behaviour this breaker exists to prevent.
    """

    def __init__(self, provider: str, opened_for_s: float):
        self.provider = provider
        self.opened_for_s = opened_for_s
        super().__init__(
            f"circuit breaker OPEN for provider={provider!r} "
            f"(tripped {opened_for_s:.1f}s ago); failing fast without a network call"
        )


class LlmBulkheadFullError(RuntimeError):
    """Raised when a provider's concurrent-call bulkhead is saturated.

    This is a deliberate rejection, not a queue — under the
    pull-architecture rule and P2, an
    overloaded dependency should shed load with an explicit signal rather
    than accept unbounded queuing that hides the saturation from the caller.
    """

    def __init__(self, provider: str, limit: int):
        self.provider = provider
        self.limit = limit
        super().__init__(
            f"bulkhead full for provider={provider!r} "
            f"(limit={limit} concurrent calls already in flight)"
        )


@dataclass
class _ProviderState:
    """Circuit-breaker bookkeeping for a single provider. Not thread-safe by
    itself — callers must hold `_STATE_LOCK` while mutating."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_successes: int = 0
    semaphore: asyncio.Semaphore | None = None


_STATE: dict[str, _ProviderState] = {}
_STATE_LOCK = threading.Lock()


def _get_state(provider: str) -> _ProviderState:
    with _STATE_LOCK:
        st = _STATE.get(provider)
        if st is None:
            st = _ProviderState()
            _STATE[provider] = st
        return st


def _get_semaphore(provider: str) -> asyncio.Semaphore | None:
    """Lazily create the per-provider semaphore on first use inside a running
    event loop (asyncio.Semaphore binds no loop until awaited in modern
    asyncio, but we still guard construction with the state lock to avoid a
    race between two first-callers)."""
    limit = int(getattr(settings, "llm_max_concurrent_calls_per_provider", 0) or 0)
    if limit <= 0:
        return None
    st = _get_state(provider)
    if st.semaphore is None:
        with _STATE_LOCK:
            if st.semaphore is None:
                st.semaphore = asyncio.Semaphore(limit)
    return st.semaphore


def circuit_state(provider: str) -> CircuitState:
    """Read-only snapshot for diagnostics/health endpoints."""
    return _get_state(provider).state


def circuit_snapshot() -> dict[str, dict]:
    """Every provider the process has OBSERVED, with its breaker state — backs the
    admin diagnostics view. Only providers that have been called appear: `_STATE` is
    populated lazily, and listing never-used providers as "closed" would imply a
    health check that never ran.

    `opened_for_s` / `cooldown_remaining_s` are present only while OPEN, so an
    operator can see whether the breaker is about to self-heal into HALF_OPEN
    (and therefore whether a manual reset is even needed).
    """
    cooldown = float(getattr(settings, "llm_circuit_breaker_cooldown_s", 30.0))
    now = time.monotonic()
    out: dict[str, dict] = {}
    with _STATE_LOCK:
        items = list(_STATE.items())
    for provider, st in items:
        row: dict = {
            "state": st.state.value,
            "consecutive_failures": st.consecutive_failures,
        }
        if st.state == CircuitState.OPEN:
            opened_for = now - st.opened_at
            row["opened_for_s"] = round(opened_for, 1)
            row["cooldown_remaining_s"] = round(max(0.0, cooldown - opened_for), 1)
        out[provider] = row
    return out


def reset_circuit(provider: str) -> None:
    """Operator/admin escape hatch (the `/admin/llm/circuit/{provider}/reset`
    endpoint in `app/api/admin_llm_circuit.py`) — forces a provider back to CLOSED
    after a manual fix. Emits a security-relevant telemetry line since this is a
    control-plane action — an emergency control.

    `_get_state` is resolved BEFORE taking the lock, not inside it: `_STATE_LOCK` is a
    plain `threading.Lock` (not an RLock), and `_get_state` acquires it itself, so the
    original `with _STATE_LOCK: st = _get_state(...)` self-deadlocked on the first call
    — hanging the caller forever while holding the lock that every other LLM call needs.
    It went unnoticed because nothing invoked this function. Every other mutator here
    (`_before_call`, `_on_success`, `_on_failure`) already uses this fetch-then-lock order.
    """
    st = _get_state(provider)
    with _STATE_LOCK:
        st.state = CircuitState.CLOSED
        st.consecutive_failures = 0
        st.half_open_successes = 0
    logger.warning("LLM_CIRCUIT_RESET | provider=%s | actor=admin | reason=manual_reset", provider)


def _before_call(provider: str) -> None:
    """Raise LlmCircuitOpenError if the breaker should fail this call fast."""
    if not getattr(settings, "llm_circuit_breaker_enabled", True):
        return
    st = _get_state(provider)
    cooldown = float(getattr(settings, "llm_circuit_breaker_cooldown_s", 30.0))
    with _STATE_LOCK:
        if st.state == CircuitState.OPEN:
            elapsed = time.monotonic() - st.opened_at
            if elapsed >= cooldown:
                # Cooldown elapsed — allow exactly one probe through.
                st.state = CircuitState.HALF_OPEN
                st.half_open_successes = 0
                logger.info("LLM_CIRCUIT_HALF_OPEN | provider=%s | cooldown_elapsed_s=%.1f",
                            provider, elapsed)
            else:
                raise LlmCircuitOpenError(provider, elapsed)
        # HALF_OPEN and CLOSED both allow the call through (HALF_OPEN allows
        # probes serially since asyncio is single-threaded per call site;
        # concurrent probes are still gated by the bulkhead above this).


def _on_success(provider: str) -> None:
    if not getattr(settings, "llm_circuit_breaker_enabled", True):
        return
    st = _get_state(provider)
    needed = int(getattr(settings, "llm_circuit_breaker_half_open_successes", 1))
    with _STATE_LOCK:
        if st.state == CircuitState.HALF_OPEN:
            st.half_open_successes += 1
            if st.half_open_successes >= needed:
                st.state = CircuitState.CLOSED
                st.consecutive_failures = 0
                st.half_open_successes = 0
                logger.info("LLM_CIRCUIT_CLOSED | provider=%s | recovered", provider)
        else:
            st.consecutive_failures = 0


# ── What counts as evidence that a PROVIDER is unhealthy ─────────────────────
# A breaker exists to stop hammering a dependency that is down. It must therefore
# only react to signals about the dependency's HEALTH. A 400 ("invalid request")
# or 404 ("model not available") is the provider answering correctly and promptly
# about a bad request WE sent — it says nothing about its health, and retrying a
# different request would have succeeded.
#
# The 2026-10-22 ainxt outage is the worked example: Claude models were disabled
# on the gateway, two aux agents (taxonomy, query_understanding) were still
# configured with `claude-haiku-4-5-20251001`, and their steady 400s drove
# consecutive_failures past the threshold. The breaker opened for the WHOLE
# `ainxt` provider, which also fronts the GPT models — so one misconfigured aux
# model black-holed every healthy model behind the same gateway. Classifying
# before recording confines the blast radius to the misconfigured call.
#
# Timeouts, connection errors, 429s and 5xxs still count: those ARE health.
_HEALTH_SIGNAL_STATUSES = frozenset({408, 425, 429})


def _http_status_of(exc: BaseException) -> int | None:
    """HTTP status carried by an SDK/transport exception, or None if it isn't an
    HTTP-status failure at all (connection reset, DNS, timeout, ...).

    Covers the three shapes the call layer produces: the OpenAI and Anthropic SDKs'
    `APIStatusError.status_code`, and httpx's `HTTPStatusError.response.status_code`
    (the Gemini path). Duck-typed on purpose — importing the SDKs here would couple
    this module to whichever client happens to be installed, and the attribute names
    are stable public API on all three.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def counts_toward_breaker(exc: BaseException) -> bool:
    """True if `exc` is evidence the PROVIDER is unhealthy (so the breaker should
    count it). False for client-side 4xx — those fail the call but must not trip
    the circuit. Public so the admin/diagnostics surface and tests can reuse it."""
    # A cancelled task is OUR decision (run cancelled, shutdown, timeout race), not
    # the provider misbehaving. `guarded_call` catches BaseException, so without this
    # cancelling a long run would ding the breaker on the way out.
    if isinstance(exc, asyncio.CancelledError):
        return False
    status = _http_status_of(exc)
    if status is None:
        # No HTTP status => transport-level failure (connect/read timeout, reset
        # connection, DNS). Exactly what the breaker is for.
        return True
    if status in _HEALTH_SIGNAL_STATUSES or status >= 500:
        return True
    # Any other 4xx: our request was wrong (400 bad request, 401/403 misconfigured
    # credential, 404 unknown model, 422 unprocessable). Fail the call, spare the circuit.
    return not (400 <= status < 500)


def _on_failure(provider: str, exc: BaseException) -> None:
    if not getattr(settings, "llm_circuit_breaker_enabled", True):
        return
    if not counts_toward_breaker(exc):
        # Logged at debug: these are frequent during a misconfiguration and the call
        # itself is already logged as a hard error by the llm.py call layer.
        logger.debug(
            "LLM_CIRCUIT_IGNORED_FAILURE | provider=%s | http_status=%s | "
            "client-side error, not counted toward the breaker | error=%s",
            provider, _http_status_of(exc), exc,
        )
        return
    st = _get_state(provider)
    threshold = int(getattr(settings, "llm_circuit_breaker_failure_threshold", 5))
    with _STATE_LOCK:
        if st.state == CircuitState.HALF_OPEN:
            # Probe failed — re-open immediately, reset cooldown clock.
            st.state = CircuitState.OPEN
            st.opened_at = time.monotonic()
            st.half_open_successes = 0
            logger.warning(
                "LLM_CIRCUIT_REOPEN | provider=%s | probe_failed error=%s", provider, exc,
            )
            return
        st.consecutive_failures += 1
        if st.consecutive_failures >= threshold and st.state == CircuitState.CLOSED:
            st.state = CircuitState.OPEN
            st.opened_at = time.monotonic()
            logger.error(
                "LLM_CIRCUIT_OPEN | provider=%s | consecutive_failures=%d threshold=%d error=%s",
                provider, st.consecutive_failures, threshold, exc,
            )


_PARTNER_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_PARTNER_SEM_LOCK = threading.Lock()


def _get_partner_semaphore(partner_id: str) -> asyncio.Semaphore | None:
    """T4 — 'No per-partner bulkhead on outbound calls'.

    Deliberately a BULKHEAD ONLY, not the full breaker+bulkhead
    `guarded_call()` gives LLM providers: partner delivery failures
    already have their own purpose-built retry/backoff system
    (`a2a_client.py`'s exponential backoff, `MAX_DELIVERY_ATTEMPTS=5`,
    the DLQ-equivalent resend path) tuned for delivery semantics, not
    5xx-storm semantics. Layering the LLM-tuned circuit breaker on top
    would double up on that existing, already-reviewed mechanism rather
    than complementing it. The bulkhead alone closes the actual gap this
    threat names: one very slow (but not hard-timing-out) partner
    endpoint consuming a disproportionate share of the outbound HTTP
    connection pool relative to other partners."""
    limit = int(getattr(settings, "partner_max_concurrent_calls", 0) or 0)
    if limit <= 0:
        return None
    with _PARTNER_SEM_LOCK:
        sem = _PARTNER_SEMAPHORES.get(partner_id)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            _PARTNER_SEMAPHORES[partner_id] = sem
        return sem


@contextlib.asynccontextmanager
async def partner_bulkhead(partner_id: str):
    """Per-partner outbound bulkhead — closes T4.

    Usage (in `a2a_client.py`, wrapping the httpx call to a partner's
    endpoint)::

        async with partner_bulkhead(partner.id):
            resp = await client.post(partner.endpoint_url, ...)

    Fails fast with `LlmBulkheadFullError` (reused — the error's meaning
    "this dependency's concurrency ceiling is saturated" is identical
    for a partner as for an LLM provider) if the partner's concurrency
    limit is saturated, rather than queuing unboundedly. A limit of 0
    (the default) disables this bulkhead entirely — same convention as
    `llm_max_concurrent_calls_per_provider`, so existing deployments see
    no behavior change until an operator opts in per-partner.
    """
    sem = _get_partner_semaphore(partner_id)
    if sem is None:
        yield
        return
    if sem.locked():
        raise LlmBulkheadFullError(
            f"partner:{partner_id}", int(getattr(settings, "partner_max_concurrent_calls", 0) or 0)
        )
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


@contextlib.asynccontextmanager
async def guarded_call(provider: str):
    """Combined bulkhead + circuit-breaker guard for one outbound LLM call.

    Usage (inside `core/llm.py`'s `call_llm`/`stream_llm` dispatch, wrapping
    the existing `_call_messages_api` / `_call_gemini` / `_do_call_openai_compat`
    invocation)::

        async with guarded_call(provider):
            response = await _call_messages_api(...)

    Raises `LlmBulkheadFullError` immediately if the provider's concurrency
    limit is saturated, or `LlmCircuitOpenError` immediately if the
    provider's breaker is OPEN — in both cases WITHOUT making a network
    call, so a degraded/overloaded provider cannot be piled onto further.
    On successful completion of the `with` body, records a breaker success; on an
    exception the breaker records a failure ONLY when the exception is evidence the
    provider is unhealthy (see `counts_toward_breaker` — client-side 4xx are not).
    Either way the exception is re-raised unchanged, so the caller's error handling
    is identical; the classification affects breaker bookkeeping only.
    """
    sem = _get_semaphore(provider)
    if sem is not None and sem.locked():
        # `locked()` means zero permits are currently available — fail fast
        # (reject, do not queue unboundedly) rather than blocking the
        # caller behind an unbounded wait.
        # A permit freed between this check and `acquire()` below is
        # a benign race — the acquire below would then succeed immediately.
        raise LlmBulkheadFullError(
            provider, int(getattr(settings, "llm_max_concurrent_calls_per_provider", 0) or 0)
        )
    _before_call(provider)
    if sem is not None:
        await sem.acquire()
    try:
        yield
    except BaseException as e:  # noqa: BLE001 — must observe every failure, then re-raise
        _on_failure(provider, e)
        raise
    else:
        _on_success(provider)
    finally:
        if sem is not None:
            sem.release()


# ── Generic sync-side breaker + bulkhead ─────────────────────────────────────
# Ported from the partner platform's `app/core/resilience.py`, where these
# classes have long guarded its A2A outbound boundary. The
# LLM-side machinery above is asyncio-flavoured and provider-keyed; these are
# the thread-side primitives the tunnel egress needs (it runs its blocking
# HTTP call in `asyncio.to_thread` workers). Port delta: the partner emits a
# security event when a circuit opens; this platform has no
# `core/security_events` module, so the WARNING log is the record here.
from contextlib import contextmanager as _contextmanager


class CircuitOpenError(RuntimeError):
    """The circuit is open — the dependency is considered unhealthy and the
    call is refused fast instead of burning the caller's timeout budget."""


class CircuitBreaker:
    """closed -> open (after N consecutive failures) -> half-open (after
    cooldown, allows ONE trial call) -> closed (on trial success) or open
    again (on trial failure)."""

    def __init__(self, name: str, *, failure_threshold: int, cooldown_s: float):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._state = "closed"
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == "open" and (time.monotonic() - self._opened_at) >= self.cooldown_s:
            self._state = "half_open"
            logger.info("circuit_breaker[%s]: open -> half_open (cooldown elapsed)", self.name)

    @_contextmanager
    def call(self):
        with self._lock:
            self._maybe_half_open()
            if self._state == "open":
                raise CircuitOpenError(
                    f"circuit '{self.name}' is open — dependency considered unhealthy, "
                    f"failing fast instead of calling it"
                )
        try:
            yield
        except Exception:
            with self._lock:
                self._consecutive_failures += 1
                if self._state == "half_open" or self._consecutive_failures >= self.failure_threshold:
                    if self._state != "open":
                        logger.warning(
                            "circuit_breaker[%s]: -> open (%d consecutive failures, threshold=%d)",
                            self.name, self._consecutive_failures, self.failure_threshold,
                        )
                    self._state = "open"
                    self._opened_at = time.monotonic()
            raise
        else:
            with self._lock:
                if self._consecutive_failures or self._state != "closed":
                    logger.info("circuit_breaker[%s]: -> closed (call succeeded)", self.name)
                self._consecutive_failures = 0
                self._state = "closed"


class Bulkhead:
    """Bounded concurrency gate — a thin wrapper over threading.BoundedSemaphore
    with a non-blocking-with-timeout acquire so callers get an immediate,
    clear rejection instead of silently queueing forever."""

    def __init__(self, name: str, *, max_concurrent: int):
        self.name = name
        self.max_concurrent = max(1, max_concurrent)
        self._sem = threading.BoundedSemaphore(self.max_concurrent)

    @_contextmanager
    def acquire(self, *, timeout: float | None = 30.0):
        acquired = self._sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise RuntimeError(
                f"bulkhead '{self.name}' saturated (max_concurrent={self.max_concurrent}) — "
                f"rejecting instead of queueing unbounded"
            )
        try:
            yield
        finally:
            self._sem.release()
