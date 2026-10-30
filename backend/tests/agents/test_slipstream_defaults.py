# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Pin the SLIPSTREAM posture (config.py): the code loop is self-directed — the
deterministic build verdict, the token budget, and the human at push approval are
the only hard stops; every analytical gate ships measure/advise. Asserted against
the CLASS defaults (not the live settings object) so a dev shell's env overrides
can't mask a regression in the shipped posture."""
from app.core.config import Settings


def _default(name):
    return Settings.model_fields[name].default


def test_analytical_gates_default_to_advise_not_block():
    for flag in (
        "agentic_approach_gate",                    # no forced stop before new API/XSD
        "agentic_contract_gate_enforce",
        "agentic_di_gate_enforce",
        "agentic_acceptance_predicates_enforce",
        "agentic_cross_module_gate_enforce",
        "agentic_tsd_approval_gate_enforce",
        "agentic_tsd_test_coverage_gate_enforce",
    ):
        assert _default(flag) is False, f"{flag} must default to advise-only (slipstream)"


def test_measure_layers_stay_on_for_signal():
    # Advise-only is not "off": the gates still run, measure, and feed the loop.
    for flag in ("agentic_acceptance_predicates", "agentic_contract_gate",
                 "agentic_di_gate", "agentic_cross_module_gate",
                 "agentic_plan_fidelity_advisory", "agentic_behavioral_gap_advisory"):
        assert _default(flag) is True, f"{flag} must stay on (measure/advise)"


def test_behavioural_opinions_are_advisory_from_round_one():
    assert _default("agentic_max_behavioral_rounds") == 0


def test_loops_are_budget_bound_not_round_capped():
    assert _default("agentic_max_code_continuations") == 64
    assert _default("agentic_max_code_attempts") == 40
    assert _default("agentic_token_budget_enforce") is True   # the bound that remains
