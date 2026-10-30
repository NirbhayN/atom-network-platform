# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Tests for the code plan schema (the live validator behind submit_plan).

Pure-dict-in, pure-dict-out — no LLM.
"""
from __future__ import annotations

from app.agents import code_plan_schema


# ──────────────────────────────────────────────────────────────────────────────
# Canonical valid plan (plan §7.3 shape)
# ──────────────────────────────────────────────────────────────────────────────

_VALID_PLAN = {
    "files": [
        {
            "path":              "src/ratelimit/TieredRateLimiter.java",
            "action":            "create",
            "intent":            "New subclass of RateLimiter that consults TenantTier before applying limits",
            "repo":              "common-infra",
            "signatures_to_add": [
                "public class TieredRateLimiter extends RateLimiter",
                "public boolean acquire(TenantContext ctx, int permits)",
            ],
            "callers_impacted":  ["PaymentRetryController.retry"],
        },
        {
            "path":   "src/retry/PaymentRetryController.java",
            "action": "modify",
            "intent": "Inject TieredRateLimiter; return 429 with Retry-After when rejected",
            "repo":   "payment-service",
        },
    ],
    "tests": [
        {
            "path":   "test/ratelimit/TieredRateLimiterTest.java",
            "action": "create",
            "cases":  [
                "enterprise_under_limit_allowed",
                "enterprise_at_limit_returns_429",
                "non_enterprise_unlimited",
            ],
        }
    ],
    "notes": "Behind feature flag TIERED_RATE_LIMIT; roll out 10%→50%→100%.",
}


# ──────────────────────────────────────────────────────────────────────────────
# validate() — happy path
# ──────────────────────────────────────────────────────────────────────────────

def test_fully_valid_plan_passes():
    report = code_plan_schema.validate(_VALID_PLAN)
    assert report["schema_valid"] is True
    assert report["file_count"] == 2
    assert report["test_count"] == 1
    assert report["issues"] == []
    assert report["missing_required"] == []


def test_minimal_valid_plan_passes():
    """Only required keys on files; tests omitted."""
    plan = {
        "files": [
            {"path": "x.java", "action": "create",
             "intent": "Add new X to satisfy spec section 3.2"},
        ],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is True
    assert report["file_count"] == 1
    assert report["test_count"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# validate() — top-level errors
# ──────────────────────────────────────────────────────────────────────────────

def test_non_dict_input_fails():
    report = code_plan_schema.validate("not a dict")
    assert report["schema_valid"] is False
    assert report["missing_required"] == ["files"]
    assert any("not a dict" in m for m in report["issues"])


def test_missing_files_key_fails():
    report = code_plan_schema.validate({"tests": []})
    assert report["schema_valid"] is False
    assert "files" in report["missing_required"]


def test_empty_files_list_fails():
    report = code_plan_schema.validate({"files": []})
    assert report["schema_valid"] is False
    assert any("at least one entry" in m for m in report["issues"])


def test_files_not_a_list_fails():
    report = code_plan_schema.validate({"files": "just a string"})
    assert report["schema_valid"] is False
    assert any("must be a list" in m for m in report["issues"])


# ──────────────────────────────────────────────────────────────────────────────
# validate() — per-file errors
# ──────────────────────────────────────────────────────────────────────────────

def test_missing_path_in_file_fails():
    plan = {"files": [{"action": "create", "intent": "some reasonable intent here"}]}
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("missing required key `path`" in m for m in report["issues"])


def test_invalid_action_fails():
    # "delete" is a LEGAL action (the live submit_plan tool contract has always
    # advertised create|modify|delete); a truly unknown verb still fails.
    ok = {"files": [{"path": "x", "action": "delete", "intent": "remove dead code from x.java"}]}
    assert code_plan_schema.validate(ok)["schema_valid"] is True
    plan = {"files": [{"path": "x", "action": "rename", "intent": "remove dead code from x.java"}]}
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("must be one of" in m for m in report["issues"])


def test_short_intent_fails():
    plan = {"files": [{"path": "x.java", "action": "create", "intent": "short"}]}
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`intent` must be a non-empty string" in m for m in report["issues"])


def test_signatures_must_be_list_when_present():
    plan = {
        "files": [
            {"path": "x.java", "action": "create",
             "intent": "OK intent here at least twelve chars",
             "signatures_to_add": "not a list"},
        ],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`signatures_to_add` must be a list" in m for m in report["issues"])


# ──────────────────────────────────────────────────────────────────────────────
# validate() — per-test errors
# ──────────────────────────────────────────────────────────────────────────────

def test_test_missing_cases_fails():
    plan = {
        "files": [{"path": "x.java", "action": "create", "intent": "OK intent here at least twelve chars"}],
        "tests": [{"path": "xt.java", "action": "create"}],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`cases` must be a non-empty list" in m for m in report["issues"]) or \
           any("missing required key `cases`" in m for m in report["issues"])


def test_test_empty_cases_fails():
    plan = {
        "files": [{"path": "x.java", "action": "create", "intent": "OK intent here at least twelve chars"}],
        "tests": [{"path": "xt.java", "action": "create", "cases": []}],
    }
    report = code_plan_schema.validate(plan)
    assert report["schema_valid"] is False
    assert any("`cases` must be a non-empty list" in m for m in report["issues"])


# The generate_plan() tests were removed with code_planner.py (BACKLOG D3): the
# one-shot planner had no production caller — the LIVE plan path is submit_plan,
# which validates against this same code_plan_schema at the door.
