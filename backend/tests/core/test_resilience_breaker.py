# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Circuit-breaker failure classification (the 2026-10-22 ainxt outage).

Claude models were disabled on the ainxt gateway while two aux agents still pointed
at `claude-haiku-4-5-20251001`. Their steady 400s tripped the per-provider breaker,
which then failed-fast every GPT call routed through the same gateway. The breaker
must only react to evidence about PROVIDER HEALTH.

Driven through the public `guarded_call` context manager wherever possible, so the
tests pin observable behaviour rather than the private bookkeeping helpers.
"""
import asyncio

import pytest

from app.core import resilience
from app.core.resilience import CircuitState, LlmCircuitOpenError, counts_toward_breaker


class _SdkStatusError(Exception):
    """Shape of openai/anthropic `APIStatusError` — a top-level `status_code`."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"status {status_code}")


class _HttpxStatusError(Exception):
    """Shape of `httpx.HTTPStatusError` — status lives on `.response` (Gemini path)."""

    def __init__(self, status_code: int):
        self.response = type("R", (), {"status_code": status_code})()
        super().__init__(f"status {status_code}")


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    """Breaker state is module-level; isolate every test."""
    resilience._STATE.clear()
    yield
    resilience._STATE.clear()


async def _fail(provider: str, exc: BaseException) -> None:
    """One guarded call that raises `exc`, swallowing it the way a caller would."""
    with pytest.raises(type(exc)):
        async with resilience.guarded_call(provider):
            raise exc


async def _ok(provider: str) -> str:
    async with resilience.guarded_call(provider):
        return "called"


def sync(coro_fn):
    """Run a test body in ONE event loop.

    Not cosmetic: the per-provider bulkhead semaphore is cached on `_ProviderState`
    and an `asyncio.Semaphore` binds to the loop that first awaits it. Calling
    `asyncio.run()` once per guarded call would hand loop 2 a semaphore owned by
    loop 1 and block forever. Production has one long-lived loop per process, so
    this is a test-harness constraint rather than a product bug.
    """
    def wrapper():
        return asyncio.run(coro_fn())
    wrapper.__name__ = coro_fn.__name__
    return wrapper


# ── Classification ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_provider_health_signals(status):
    assert counts_toward_breaker(_SdkStatusError(status)) is False
    assert counts_toward_breaker(_HttpxStatusError(status)) is False


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_rate_limits_and_server_errors_are_health_signals(status):
    assert counts_toward_breaker(_SdkStatusError(status)) is True
    assert counts_toward_breaker(_HttpxStatusError(status)) is True


def test_transport_errors_without_a_status_are_health_signals():
    # Connection reset / DNS / read timeout carry no HTTP status — exactly what the
    # breaker exists for, so an unrecognised exception must count (fail safe).
    assert counts_toward_breaker(ConnectionError("connection reset by peer")) is True
    assert counts_toward_breaker(TimeoutError("read timeout")) is True
    assert counts_toward_breaker(RuntimeError("something unexpected")) is True


def test_cancellation_is_not_a_provider_failure():
    # guarded_call catches BaseException, so a cancelled run would otherwise ding the
    # breaker on its way out.
    assert counts_toward_breaker(asyncio.CancelledError()) is False


def test_malformed_status_attribute_is_treated_as_transport():
    exc = _SdkStatusError(0)
    exc.status_code = "not-a-number"
    assert counts_toward_breaker(exc) is True


# ── End-to-end through guarded_call ───────────────────────────────────────────

@sync
async def test_repeated_400s_never_open_the_circuit():
    """The outage, reproduced: a misconfigured model 400s far past the threshold."""
    threshold = int(resilience.settings.llm_circuit_breaker_failure_threshold)
    for _ in range(threshold * 3):
        await _fail("ainxt", _SdkStatusError(400))

    assert resilience.circuit_state("ainxt") == CircuitState.CLOSED
    assert resilience._get_state("ainxt").consecutive_failures == 0

    # And the healthy models behind the same gateway still get through.
    assert await _ok("ainxt") == "called"


@sync
async def test_repeated_5xx_still_open_the_circuit():
    """The breaker must not be defanged — genuine provider failure still trips it."""
    threshold = int(resilience.settings.llm_circuit_breaker_failure_threshold)
    for _ in range(threshold):
        await _fail("ainxt", _SdkStatusError(503))

    assert resilience.circuit_state("ainxt") == CircuitState.OPEN
    with pytest.raises(LlmCircuitOpenError):
        await _ok("ainxt")


@sync
async def test_client_errors_do_not_reopen_a_half_open_circuit():
    """A HALF_OPEN probe that 400s proved nothing about health — don't re-open."""
    st = resilience._get_state("ainxt")
    st.state = CircuitState.HALF_OPEN
    st.half_open_successes = 0

    await _fail("ainxt", _SdkStatusError(400))
    assert resilience.circuit_state("ainxt") == CircuitState.HALF_OPEN

    # A real failure on the probe still re-opens it.
    await _fail("ainxt", _SdkStatusError(503))
    assert resilience.circuit_state("ainxt") == CircuitState.OPEN


@sync
async def test_one_providers_failures_do_not_trip_another():
    threshold = int(resilience.settings.llm_circuit_breaker_failure_threshold)
    for _ in range(threshold):
        await _fail("ainxt", _SdkStatusError(503))
    assert resilience.circuit_state("ainxt") == CircuitState.OPEN
    assert resilience.circuit_state("anthropic") == CircuitState.CLOSED


# ── Snapshot + reset (backs the admin endpoint) ───────────────────────────────

@sync
async def test_snapshot_reports_open_state_with_cooldown_remaining():
    threshold = int(resilience.settings.llm_circuit_breaker_failure_threshold)
    for _ in range(threshold):
        await _fail("ainxt", _SdkStatusError(503))

    snap = resilience.circuit_snapshot()
    assert snap["ainxt"]["state"] == "open"
    assert snap["ainxt"]["consecutive_failures"] >= threshold
    assert snap["ainxt"]["cooldown_remaining_s"] >= 0


def test_snapshot_omits_never_called_providers():
    assert "gemini" not in resilience.circuit_snapshot()


@sync
async def test_reset_closes_an_open_circuit():
    threshold = int(resilience.settings.llm_circuit_breaker_failure_threshold)
    for _ in range(threshold):
        await _fail("ainxt", _SdkStatusError(503))
    assert resilience.circuit_state("ainxt") == CircuitState.OPEN

    resilience.reset_circuit("ainxt")

    assert resilience.circuit_state("ainxt") == CircuitState.CLOSED
    assert resilience.circuit_snapshot()["ainxt"]["consecutive_failures"] == 0
    assert await _ok("ainxt") == "called"
