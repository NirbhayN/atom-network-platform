# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Admin Phase-B outcome metrics (BACKLOG E1).

One read-only endpoint over :mod:`app.services.agentic_metrics` so operators can
see — without reading transcripts — whether the agentic pipeline is actually
getting better: success rate, verification outcomes (including NEVER-BUILT
completions), review pressure, and how often each advisory gate fires (the
shadow-mode evidence for hardening or deleting a gate)."""
import logging

from fastapi import APIRouter

from app.core.deps import AdminUser, DbDep
from app.services.agentic_metrics import collect_metrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/agentic", tags=["admin-agentic"])


@router.get("/metrics")
def agentic_outcome_metrics(db: DbDep, _: AdminUser, days: int = 30, limit: int = 500):
    """Aggregated run outcomes for the window (default: last 30 days, ≤500 runs)."""
    return collect_metrics(db, days=days, limit=limit)
