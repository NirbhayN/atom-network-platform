# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Meta-tests: does the harness actually detect a regression?

A harness nobody has watched fail is not a harness — it is a source of false
confidence, which is worse than no harness because it gets cited as evidence.
So the load-bearing test here is not "the golden scores well". It is
"the degraded document scores badly", and specifically that it is caught by the
layer that would notice the regression genericization really causes.
"""
import pytest

from tests.golden.runner import FIXTURES, golden_text, list_cases, load_case, score_candidate
from tests.golden.scoring import section_coverage, specificity, structural_score

CASE = "case_001"


def _degraded() -> str:
    return (FIXTURES / f"{CASE}.degraded.md").read_text(encoding="utf-8")


def test_fixtures_are_discoverable():
    assert CASE in list_cases()


def test_golden_scores_above_its_own_floor():
    case = load_case(CASE)
    score = score_candidate(CASE, golden_text(CASE))
    assert score.value >= case["thresholds"]["structural_min"], str(score)


def test_degraded_document_is_caught():
    """THE test. If this ever passes, the suite has stopped measuring."""
    case = load_case(CASE)
    score = score_candidate(CASE, _degraded())
    assert score.value < case["thresholds"]["structural_min"], (
        f"degraded fixture scored {score.value:.3f}, at or above the floor — "
        "the harness would not catch a real regression"
    )


def test_section_coverage_alone_would_have_missed_it():
    """Why the specificity layer exists.

    The degraded document keeps every heading, so structure-only scoring rates
    it perfect. That is precisely the failure mode of removing "be specific to
    the domain" from a prompt: the skeleton survives and the substance does
    not. A harness measuring only structure would have signed this off.
    """
    golden = golden_text(CASE)
    degraded = _degraded()

    assert section_coverage(degraded, golden).value == pytest.approx(1.0)

    terms = load_case(CASE)["domain_terms"]
    assert specificity(degraded, terms).value < 0.5
    assert specificity(golden, terms).value == pytest.approx(1.0)


def test_findings_name_what_is_missing():
    """A score with no explanation cannot be acted on."""
    score = score_candidate(CASE, _degraded())
    assert score.findings
    joined = " ".join(score.findings).lower()
    assert "domain term absent" in joined


def test_placeholders_are_penalised():
    """Reuses the platform's own deterministic gate, so a golden run and a real
    run never disagree about the rules — only about the model output."""
    golden = golden_text(CASE)
    clean = structural_score(golden, golden, "brd", [])
    dirty = structural_score(golden + "\n\nTBD: fill this in later.\n", golden, "brd", [])
    assert dirty.value < clean.value


def test_empty_candidate_scores_zero_ish_and_does_not_raise():
    score = score_candidate(CASE, "")
    assert score.value < 0.2
    assert score.findings


def test_structural_scoring_is_offline():
    """The FREE path must never need credentials or a network.

    Scoped to the structural functions on purpose: semantic_score legitimately
    calls an LLM. If the structural half ever acquires that dependency it stops
    being runnable in CI, and a suite that cannot run is not a suite.
    """
    import inspect

    from tests.golden import scoring

    for fn in (scoring.structural_score, scoring.section_coverage,
               scoring.specificity, scoring.deterministic_findings):
        src = inspect.getsource(fn)
        body = "\n".join(ln for ln in src.splitlines()
                          if not ln.strip().startswith("#"))
        for forbidden in ("requests.", "httpx.", "urllib.request", "call_llm("):
            assert forbidden not in body, (
                f"{fn.__name__} reached for {forbidden}")


def test_semantic_scoring_reports_unavailable_rather_than_scoring_zero(monkeypatch):
    """"Not measured" must never be recorded as "measured and fine", nor as
    "measured and bad" — both hide regressions, in opposite directions."""
    from app.core.config import settings

    from tests.golden import scoring

    monkeypatch.setattr(settings, "llm_provider", "", raising=False)
    assert scoring.semantic_score("a", "b", model="pinned-model") is None


# ── E2: XSD + code-plan golden cases ────────────────────────────────────────────────

def test_xsd_golden_compiles_and_scores_above_floor():
    from tests.golden.runner import FIXTURES
    case = load_case("case_003")
    score = score_candidate("case_003", golden_text("case_003"))
    assert score.value >= case["thresholds"]["structural_min"], str(score)
    assert not any("schema compile" in f for f in score.findings), str(score)


def test_xsd_degraded_is_caught_by_sections_and_compile():
    from tests.golden.runner import FIXTURES
    case = load_case("case_003")
    degraded = (FIXTURES / "case_003.degraded.md").read_text(encoding="utf-8")
    score = score_candidate("case_003", degraded)
    assert score.value < case["thresholds"]["structural_min"], str(score)
    # The broken xml block is flagged by the stdlib well-formedness tier, so this
    # holds even on hosts without the optional xmlschema dependency.
    assert any("schema compile" in f for f in score.findings), str(score)


def test_plan_golden_scores_full_and_degraded_is_caught():
    import json
    from tests.golden.runner import FIXTURES
    from tests.golden.scoring import plan_structural_score
    plans = FIXTURES / "plans"
    golden = json.loads((plans / "plan_case_001.golden.json").read_text(encoding="utf-8"))
    degraded = json.loads((plans / "plan_case_001.degraded.json").read_text(encoding="utf-8"))
    assert plan_structural_score(golden, golden).value == 1.0
    score = plan_structural_score(degraded, golden)
    assert score.value < 0.5, str(score)                      # drift halves + dropped file
    assert any("missing planned file" in f for f in score.findings)
    assert any(f.startswith("schema:") for f in score.findings)
