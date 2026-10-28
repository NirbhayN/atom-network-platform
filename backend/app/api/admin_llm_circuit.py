# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Admin LLM circuit-breaker API — inspect and manually close a tripped breaker.

The per-provider breaker in `app/core/resilience.py` fails calls fast while a
provider looks unhealthy, and self-heals via HALF_OPEN once the cooldown elapses.
That covers a provider that actually recovers on its own. It does NOT cover the
case the 2026-10-22 ainxt outage produced: the breaker opened because of a
CONFIG mistake, so every HALF_OPEN probe kept failing for the same reason and the
circuit re-opened forever. `reset_circuit()` existed for exactly that, but nothing
called it — leaving "restart the backend" as the only recovery, which is a blunt
instrument on a worker mid-run.

State is process-local by design (see the module docstring in `resilience.py`), so
a reset only affects the process that serves the request. With more than one
backend/worker process, an operator must reset on each, or restart. The response
says so explicitly rather than implying a cluster-wide effect.
"""
import logging
import os
import socket

from fastapi import APIRouter, HTTPException

from app.core import resilience
from app.core.deps import AdminUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/llm/circuit", tags=["admin-llm-circuit"])

# Guard the path parameter. `reset_circuit` lazily CREATES state for whatever key it
# is handed, so an unvalidated provider name would let a typo ("anthropc") silently
# return 200 having reset a phantom provider the operator then believes is fixed.
_KNOWN_PROVIDERS = frozenset({"anthropic", "openai", "ainxt", "ollama", "gemini"})


def _process_id() -> dict:
    """Which worker answered — an operator resetting a multi-process deployment
    needs to know whether they have covered them all."""
    return {"host": socket.gethostname(), "pid": os.getpid()}


@router.get("")
def get_circuits(_: AdminUser):
    """Breaker state for every provider this process has called."""
    return {
        "process": _process_id(),
        "providers": resilience.circuit_snapshot(),
        "note": "Breaker state is per process. In a multi-worker deployment this "
                "reflects only the worker that served this request.",
    }


@router.post("/{provider}/reset")
def reset_circuit(provider: str, _: AdminUser):
    """Force `provider` back to CLOSED after fixing the underlying problem."""
    key = provider.strip().lower()
    if key not in _KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown provider {provider!r}. Known: {', '.join(sorted(_KNOWN_PROVIDERS))}",
        )
    before = resilience.circuit_snapshot().get(key, {"state": "closed", "consecutive_failures": 0})
    resilience.reset_circuit(key)
    logger.warning(
        "LLM_CIRCUIT_RESET_API | provider=%s | previous_state=%s | host=%s pid=%s",
        key, before.get("state"), socket.gethostname(), os.getpid(),
    )
    return {
        "provider": key,
        "previous": before,
        "current": resilience.circuit_snapshot().get(key, {"state": "closed"}),
        "process": _process_id(),
        "note": "Reset applies to this worker process only. Repeat per worker, or "
                "restart, if the deployment runs more than one.",
    }
