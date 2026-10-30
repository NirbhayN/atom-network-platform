# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""XSD Generator agent.

Determines whether schema changes are required for a network feature, then generates
the updated XSD with diff annotations. Handles iterative feedback.
"""
import logging
import re
from app.core.prompts import render_prompt
from collections.abc import AsyncGenerator

from app.core.llm import stream_llm
from app.core.domain.registry import prompt_block

# Supplied by the active domain pack, not imported from a domain-specific module.
NETWORK_HARD_RULES = prompt_block("hard_rules")
from app.agents._prompt_safety import wrap_untrusted, ANTI_INJECTION_CLAUSE

logger = logging.getLogger(__name__)

ASSESSMENT_SYSTEM_PROMPT = render_prompt(
    "agents/xsd/assessment_system_prompt.md",
    NETWORK_HARD_RULES=NETWORK_HARD_RULES, ANTI_INJECTION_CLAUSE=ANTI_INJECTION_CLAUSE,
    PLATFORM_NAME=prompt_block("platform_name", "this change-management platform"),
    DOMAIN_NAME=prompt_block("domain_name", "this domain"),
    # `collect_request.xsd` is a payment message type. Naming it here told the
    # analyst which schema FILES this ecosystem has, so a non-payments run
    # answered "Affected Schemas" with files that do not exist in its contract.
    # The neutral default names a shape rather than a real schema.
    SCHEMA_FILE_EXAMPLES=prompt_block(
        "xsd_schema_file_examples", "`<message>_request.xsd`, `<message>_response.xsd`"),
)

XSD_GENERATION_SYSTEM_PROMPT = render_prompt(
    "agents/xsd/xsd_generation_system_prompt.md",
    NETWORK_HARD_RULES=NETWORK_HARD_RULES, ANTI_INJECTION_CLAUSE=ANTI_INJECTION_CLAUSE,
    PLATFORM_NAME=prompt_block("platform_name", "this change-management platform"),
    DOMAIN_NAME=prompt_block("domain_name", "this domain"),
    AUTHORITY=prompt_block("authority", "the platform operator"),
)


_HEADING_SPLIT_RE = re.compile(r"(?m)(?=^#{1,6}\s)")


def _clip_markdown(text: str, max_chars: int, label: str) -> str:
    """Section-aware clip (B3): keep WHOLE markdown sections in order until the budget,
    then list the omitted headings — a mid-sentence `[:N]` slice silently dropped
    fields/sections and nothing ever said so. Clipping is logged and visible to the
    model (it can ask for or reason about the named omissions instead of not knowing
    they exist). A document with no headings degrades to a hard clip with the note."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    kept: list[str] = []
    used = 0
    omitted: list[str] = []
    for part in _HEADING_SPLIT_RE.split(text):
        if not part:
            continue
        if used + len(part) <= max_chars and not omitted:   # keep order; stop at first miss
            kept.append(part)
            used += len(part)
        else:
            head = (part.strip().splitlines() or ["(unnamed section)"])[0]
            omitted.append(head.lstrip("# ").strip()[:80])
    if not kept:                                    # headingless doc, or first section too big
        kept, omitted = [text[:max_chars]], ["(document tail)"]
    note = (f"\n\n[{label} clipped to fit the prompt: {len(omitted)} section(s) omitted — "
            + "; ".join(omitted[:10]) + ("…" if len(omitted) > 10 else "") + "]\n")
    logger.warning("xsd prompt: %s clipped %d → ~%d chars (%d section(s) omitted)",
                   label, len(text), used, len(omitted))
    return "".join(kept) + note


def _context_block(tech_spec_content: str, brd_content: str, base_schemas: str = "") -> str:
    """The grounding context for every XSD call: spec + BRD + (B1) the CURRENT
    schemas the change touches. Without the base schemas the model was asked to
    'keep existing elements intact' for files it had never seen — it reconstructed
    them from memory, the single largest XSD-accuracy loss."""
    from app.core.config import settings
    spec_cap = int(getattr(settings, "xsd_prompt_tech_spec_chars", 16_000))
    brd_cap = int(getattr(settings, "xsd_prompt_brd_chars", 8_000))
    context = f"""TECHNICAL SPECIFICATION:
{wrap_untrusted(_clip_markdown(tech_spec_content, spec_cap, "tech spec"), "TECH_SPEC_CONTENT")}

---
BRD (summary):
{wrap_untrusted(_clip_markdown(brd_content, brd_cap, "BRD"), "BRD_CONTENT")}
"""
    if base_schemas:
        context += (
            "\n---\nCURRENT SCHEMAS (authoritative BASE content, verbatim from the ecosystem):\n"
            f"{wrap_untrusted(base_schemas, 'CURRENT_SCHEMAS')}\n"
            "Ground every schema edit in the files above: PRESERVE existing elements, types, "
            "namespaces and naming conventions EXACTLY, and modify only what this change "
            "requires. Never re-author a schema from memory — if a base file you need is not "
            "shown above, say so in the Summary instead of guessing its contents.\n")
    return context


async def stream_xsd_assessment(
    tech_spec_content: str,
    brd_content: str,
    base_schemas: str = "",
) -> AsyncGenerator[str, None]:
    """Assess whether XSD changes are required."""
    message = ("Analyse the following Technical Specification and BRD to determine\n"
               "whether XSD changes are required.\n\n"
               + _context_block(tech_spec_content, brd_content, base_schemas))
    logger.info("XSDAgent — streaming assessment (schema_ctx_chars=%d)", len(base_schemas))

    async for chunk in stream_llm(system=ASSESSMENT_SYSTEM_PROMPT, messages=[{"role": "user", "content": message}], max_tokens=2048, agent_name="xsd"):
        yield chunk


async def stream_xsd_turn(
    tech_spec_content: str,
    brd_content: str,
    conversation_history: list[dict],
    new_user_message: str,
    base_schemas: str = "",
) -> AsyncGenerator[str, None]:
    """Stream an XSD generation or feedback refinement turn."""
    context = _context_block(tech_spec_content, brd_content, base_schemas)
    if len(conversation_history) == 0:
        messages = [{"role": "user", "content": f"{context}\n\n---\n{wrap_untrusted(new_user_message, 'USER_MESSAGE')}"}]
    else:
        # The persisted conversation keeps only the bare user/assistant texts — turn 1's
        # ephemeral context (spec, BRD, base schemas) is NOT in it, so refine/repair turns
        # used to run entirely WITHOUT the specification. Re-anchor every multi-turn call
        # with a leading context exchange (an assistant ack keeps role alternation valid).
        messages = ([{"role": "user", "content": context},
                     {"role": "assistant",
                      "content": "Understood — I have the specification, BRD and current schemas."}]
                    + conversation_history
                    + [{"role": "user", "content": wrap_untrusted(new_user_message, "USER_MESSAGE")}])

    logger.info("XSDAgent — streaming turn, history_len=%d schema_ctx_chars=%d",
                len(messages), len(base_schemas))

    # max_tokens bumped 8096 → 24000 (2026-05-04, Layer-3 of truncation fix).
    # XSD outputs include full schema body + commentary; previously hit cap.
    async for chunk in stream_llm(system=XSD_GENERATION_SYSTEM_PROMPT, messages=messages, max_tokens=24000, agent_name="xsd"):
        yield chunk


# B4 — structured REQUIRED/NOT-REQUIRED decision. The assessment prompt mandates a
# `### Decision` section whose body is `**REQUIRED**` or `**NOT REQUIRED**`; parse
# THAT section instead of substring-matching the whole document (where a rationale
# like "a REQUIRED field was added" used to flip the verdict).
_DECISION_SECTION_RE = re.compile(
    r"^#{1,6}\s*Decision\b[^\n]*\n(.{0,400}?)(?=^#{1,6}\s|\Z)", re.I | re.M | re.S)


def parse_xsd_decision(assessment_text: str) -> bool:
    """True when the assessment decides schema changes ARE required. Reads the
    `### Decision` section first; falls back to the legacy whole-document heuristic
    (logged) only when no parseable Decision section exists."""
    text = assessment_text or ""
    m = _DECISION_SECTION_RE.search(text)
    if m:
        body = m.group(1)
        if re.search(r"\bNOT\s+REQUIRED\b", body, re.I):
            return False
        if re.search(r"\bREQUIRED\b", body, re.I):
            return True
    logger.warning("xsd decision: no parseable Decision section — using the legacy heuristic")
    return "**REQUIRED**" in text and "NOT REQUIRED" not in text.split("**REQUIRED**")[0]
