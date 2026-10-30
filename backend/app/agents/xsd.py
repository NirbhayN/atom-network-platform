# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""XSD Generator agent.

Determines whether schema changes are required for a network feature, then generates
the updated XSD with diff annotations. Handles iterative feedback.
"""
import logging
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


def _context_block(tech_spec_content: str, brd_content: str, base_schemas: str = "") -> str:
    """The grounding context for every XSD call: spec + BRD + (B1) the CURRENT
    schemas the change touches. Without the base schemas the model was asked to
    'keep existing elements intact' for files it had never seen — it reconstructed
    them from memory, the single largest XSD-accuracy loss."""
    context = f"""TECHNICAL SPECIFICATION:
{wrap_untrusted(tech_spec_content[:4000] + ('...' if len(tech_spec_content) > 4000 else ''), "TECH_SPEC_CONTENT")}

---
BRD (summary):
{wrap_untrusted(brd_content[:2000] + ('...' if len(brd_content) > 2000 else ''), "BRD_CONTENT")}
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
