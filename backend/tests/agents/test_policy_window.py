# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Regression tests for `_policy_window` (app.agents.agentic_runtime).

The orchestrator threads ``model=None`` through the agent loop and lets the
call layer resolve the real id per-request. `_policy_window(None)` used to
skip the per-model window table and land on the PROVIDER fallback — for the
ainxt gateway that is 128K, which silently clamped a 600K configured policy
to 128K and drove near-constant history compaction (observed: 98 compactions
consuming 75% of a 185-minute run). It must now resolve the same model the
call layer will use before clamping.
"""
from __future__ import annotations

from app.agents.agentic_runtime import _policy_window
from app.core.config import settings


def test_none_model_resolves_configured_model_not_provider_fallback(monkeypatch):
    # The exact production shape of the incident: ainxt gateway in Anthropic
    # compat mode fronting claude-sonnet-5 (1M window) with a 600K policy.
    monkeypatch.setattr(settings, "llm_provider", "ainxt")
    monkeypatch.setattr(settings, "ainxt_compat_mode", "ANTHROPIC")
    monkeypatch.setattr(settings, "ainxt_messages_model", "claude-sonnet-5")
    monkeypatch.setattr(settings, "agentic_context_window_tokens", 600_000)
    assert _policy_window(None) == 600_000      # was 128_000 (ainxt provider fallback)


def test_explicit_small_model_still_clamps_policy(monkeypatch):
    # The clamp exists for a reason: a 600K policy on a 200K model must shrink
    # to the model's real window, not fire after it has already overflowed.
    monkeypatch.setattr(settings, "agentic_context_window_tokens", 600_000)
    assert _policy_window("claude-haiku-4-5") == 200_000


def test_policy_smaller_than_model_window_wins(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "claude_model", "claude-sonnet-5")
    monkeypatch.setattr(settings, "agentic_context_window_tokens", 300_000)
    assert _policy_window(None) == 300_000
