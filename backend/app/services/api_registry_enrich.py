# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Agent enrichment of registry field descriptions (the TSD "Message Item" column).

The XSD ingest fills every structural cell (tag num, occurrence, datatype,
length, mandatory) but has no prose source — `message_item` is empty on a
fresh registry, so registry-rendered TSD tables ship a blank description
column. Historically that prose existed only because the LLM authored whole
API tables (hallucination-prone, replaced by registry rendering) or because a
human PATCHed fields one by one.

This module restores agent-written descriptions WITHOUT reopening the
model-generated-tables hole: the agent writes ONCE into the registry — the
same editable cell a human uses — where the text is reviewable and durable.
Document rendering stays fully deterministic.

Write semantics, deliberately different from the human PATCH endpoint:
  * `updated_by` is NOT set — the human-edit lock would freeze the field's
    XSD-derived cells against future re-ingests. Ingest never touches
    `message_item`, so agent descriptions survive re-ingests anyway.
  * A field a human HAS locked (`updated_by` set) is never overwritten —
    human text always wins.
  * Provenance lands in `constraint_sources["agent"]`, next to the "xsd" and
    "manual" evidence the registry already keeps.
"""
from __future__ import annotations

import logging

from app.agents._prompt_safety import ANTI_INJECTION_CLAUSE, wrap_untrusted
from app.core.domain.registry import prompt_block

logger = logging.getLogger(__name__)

# Longest description accepted into the registry cell. The reference-spec
# style ("Version of the API", "Time of request from the creator of the
# message") is far shorter; the cap only guards against a runaway model.
_MAX_DESCRIPTION_CHARS = 200

_SCHEMA = {
    "type": "object",
    "properties": {
        "descriptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "xpath": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["xpath", "description"],
            },
        },
    },
    "required": ["descriptions"],
}


def _system_prompt() -> str:
    domain_notes = prompt_block("docgen_generic_domain_notes", "")
    return (
        "You write the 'Message Item' column of an official wire-API specification "
        "table: for each field of a message, a short description of WHAT the field "
        "is, in the style of 'Version of the API', 'Time of request from the creator "
        "of the message', 'Unique identifier of the transaction'.\n"
        "Rules:\n"
        "- At most 12 words; a noun phrase, not a sentence.\n"
        "- Describe only what the field IS. Never state lengths, datatypes, "
        "occurrence or allowed values — those live in other columns.\n"
        "- Infer meaning only from the tag name, its position in the message and "
        "the sample XML. When the meaning is not inferable, return a plain "
        "expansion of the tag name itself rather than a guess.\n"
        "- Return one entry per requested xpath, with the xpath copied verbatim.\n"
        + (f"\nDOMAIN CONTEXT:\n{domain_notes}\n" if domain_notes.strip() else "")
        + ANTI_INJECTION_CLAUSE
    )


def _candidate_fields(fields, overwrite: bool):
    """Fields the agent may describe: active, never human-locked, and —
    unless overwriting — not already described."""
    out = []
    for f in fields:
        if f.updated_by is not None:
            continue
        if (f.message_item or "").strip() and not overwrite:
            continue
        out.append(f)
    return out


def _apply_descriptions(fields, by_xpath: dict[str, str]) -> int:
    """Write agent descriptions onto `fields` (no commit). Returns count written."""
    written = 0
    for f in fields:
        desc = (by_xpath.get(f.xpath) or "").strip()
        if not desc:
            continue
        f.message_item = desc[:_MAX_DESCRIPTION_CHARS]
        cs = dict(f.constraint_sources or {})
        cs["agent"] = {"message_item": "description_enrichment"}
        f.constraint_sources = cs
        written += 1
    return written


async def enrich_message_items(db, api_names: list[str] | None = None,
                               overwrite: bool = False) -> dict:
    """Fill empty `message_item` cells across the registry with one LLM call per
    message. Fail-open per message: an LLM error skips that message and the
    sweep continues. Returns counts for the admin response."""
    from app.core.llm import call_llm_structured
    from app.models.api_registry import ApiField, ApiMessage

    q = db.query(ApiMessage).filter(ApiMessage.status == "active")
    if api_names:
        q = q.filter(ApiMessage.api_name.in_(api_names))
    messages = q.order_by(ApiMessage.api_name).all()

    counts = {"messages_processed": 0, "messages_failed": 0,
              "fields_described": 0, "human_locked_skipped": 0,
              "already_filled_skipped": 0}
    system = _system_prompt()

    for msg in messages:
        fields = (db.query(ApiField)
                  .filter(ApiField.message_id == msg.id, ApiField.status == "active")
                  .order_by(ApiField.position).all())
        counts["human_locked_skipped"] += sum(1 for f in fields if f.updated_by is not None)
        candidates = _candidate_fields(fields, overwrite)
        counts["already_filled_skipped"] += sum(
            1 for f in fields
            if f.updated_by is None and (f.message_item or "").strip() and not overwrite)
        if not candidates:
            continue

        field_lines = "\n".join(
            f"{f.xpath} | tag={'@' + f.xml_tag if f.is_attribute else f.xml_tag}"
            f" | occurrence={f.occurrence or '?'} | mandatory={f.mandatory or '?'}"
            for f in candidates)
        user = (
            f"MESSAGE: {msg.api_name}\n\n"
            f"SAMPLE XML:\n{wrap_untrusted((msg.sample_xml or '')[:4000], 'SAMPLE_XML')}\n\n"
            f"FIELDS (one description per line, xpath verbatim):\n"
            f"{wrap_untrusted(field_lines[:8000], 'FIELD_LIST')}\n\n"
            "Write the Message Item description for every listed xpath."
        )
        try:
            data = await call_llm_structured(
                system, user,
                schema=_SCHEMA,
                agent_name="registry_description_enrichment",
                tool_name="record_descriptions",
                max_tokens=min(4000, 200 + 40 * len(candidates)),
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, enrich the rest
            logger.warning("registry enrichment: LLM failed for %s: %s", msg.api_name, exc)
            counts["messages_failed"] += 1
            continue
        entries = (data or {}).get("descriptions")
        if not isinstance(entries, list):
            logger.warning("registry enrichment: no usable output for %s", msg.api_name)
            counts["messages_failed"] += 1
            continue
        by_xpath = {str(e.get("xpath", "")): str(e.get("description", ""))
                    for e in entries if isinstance(e, dict)}
        counts["fields_described"] += _apply_descriptions(candidates, by_xpath)
        counts["messages_processed"] += 1
        db.commit()  # per-message: a later failure keeps earlier descriptions

    return counts
