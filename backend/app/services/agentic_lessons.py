# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Cross-run lessons (gap G2) — the agent writes its own memory.

Within one change, facts ride the fact sheet and the phase handoff; when the run
completes they die, and the next change relearns "this repo's build needs X" from
scratch. This module closes the loop: at COMPLETED, an LLM distills the run's
durable, repo-general gotchas (build quirks, hidden couplings, naming traps —
never change-specific facts) into per-repo lesson lines, stored in the existing
``RepoPathContext`` table under ``AGENT_LESSONS.md`` (no migration; same
context-as-asset channel as MODULE_NOTES). Future runs inject them into the code
agent's prompt as advisory, verify-before-relying context.

Properties: fail-open everywhere (a distillation failure never touches the run),
deduplicated on normalized text, capped FIFO (newest survive), and the store is
operator-editable — delete a wrong lesson and it stays gone unless re-learned.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

LESSONS_PATH = "AGENT_LESSONS.md"
_MAX_LESSON_CHARS = 200
_LINE_RE = re.compile(r"^\s*-\s+(.*\S)\s*$")

_SYSTEM = (
    "You distill DURABLE engineering lessons from one automated coding run, for the "
    "benefit of FUTURE runs on the same repositories. A lesson is repo-general: a build "
    "quirk, a hidden coupling, a naming/convention trap, a process gotcha. It is NOT a "
    "fact about this specific change (no feature names, ticket details, or one-off "
    "values). Fewer, sharper lessons beat many vague ones; output [] when the run "
    "taught nothing durable. Never invent — only distill what the material shows."
)


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip().lower())


def parse_lessons(content: str) -> list[str]:
    return [m.group(1) for ln in (content or "").splitlines() if (m := _LINE_RE.match(ln))]


def merge_lessons(existing: str, new_lessons: list[str], cap: int) -> tuple[str, int]:
    """Pure merge: dedupe (normalized), append new, keep the NEWEST ``cap`` lines."""
    lines = parse_lessons(existing)
    seen = {_norm(l) for l in lines}
    added = 0
    for lesson in new_lessons:
        lesson = (lesson or "").strip()[:_MAX_LESSON_CHARS]
        if lesson and _norm(lesson) not in seen:
            lines.append(lesson)
            seen.add(_norm(lesson))
            added += 1
    lines = lines[-max(1, cap):]
    return "\n".join(f"- {l}" for l in lines), added


def load_lessons(db, repo_ids: list[str]) -> dict[str, str]:
    """{repo_id: lessons markdown} for injection into the context pack. Fail-open."""
    try:
        from app.models.module_context import RepoPathContext
        rows = (db.query(RepoPathContext)
                .filter(RepoPathContext.repo_id.in_(list(repo_ids or [])),
                        RepoPathContext.path == LESSONS_PATH).all())
        return {r.repo_id: (r.content or "") for r in rows if (r.content or "").strip()}
    except Exception as e:  # noqa: BLE001 — advisory context, never blocks assembly
        logger.warning("lessons load failed (fail-open): %s", e)
        return {}


def _material(db, run) -> str:
    """What the run can teach from: the fact sheet + the failure/review history."""
    from app.models.agentic import AgenticEvent

    parts: list[str] = []
    facts = ((run.handoff_json or {}).get("facts") or [])
    if facts:
        parts.append("FACTS the agent recorded during the run:\n" + "\n".join(
            f"- {str(f.get('fact') or f)[:300]}" for f in facts[:30] if f))
    ev = (db.query(AgenticEvent.kind, AgenticEvent.payload)
          .filter(AgenticEvent.run_id == run.id,
                  AgenticEvent.kind.in_(("verification", "java_version_switch",
                                         "java_version_warning", "review")))
          .order_by(AgenticEvent.seq).limit(60).all())
    for kind, payload in ev:
        pl = payload or {}
        if kind == "verification" and pl.get("errors"):
            parts.append("BUILD ERRORS seen (later fixed):\n" + "\n".join(
                f"- {str(e)[:240]}" for e in pl["errors"][:8]))
        elif kind in ("java_version_switch", "java_version_warning"):
            parts.append(f"TOOLCHAIN: {json.dumps(pl, default=str)[:300]}")
    return "\n\n".join(parts)[:16_000]


async def distill_for_run(run_id: str) -> int:
    """Isolated-session distillation; returns the number of lessons stored."""
    from app.core.config import settings
    from app.core.database import SessionLocal
    from app.core.llm import call_llm
    from app.models.agentic import AgenticRun
    from app.models.module_context import RepoPathContext

    db = SessionLocal()
    try:
        run = db.get(AgenticRun, run_id)
        if run is None:
            return 0
        repo_ids = list(run.selected_repo_ids or [])
        if not repo_ids:
            return 0
        material = _material(db, run)
        if not material.strip():
            return 0
        user = (f"Repositories in this run: {', '.join(repo_ids)}\n\n{material}\n\n"
                "Extract 0-3 DURABLE lessons for future runs on these repositories. "
                "Each lesson ≤160 chars, imperative, standalone. Output STRICT JSON "
                "only: a list of {\"repo_id\": \"<one of the ids above>\", "
                "\"lesson\": \"...\"} — or [] if nothing durable was learned.")
        raw = await call_llm(system=_SYSTEM, messages=[{"role": "user", "content": user}],
                             max_tokens=600, agent_name="lessons")
        try:
            items = json.loads((raw or "").strip().strip("`").removeprefix("json"))
        except Exception:  # noqa: BLE001 — unparseable output teaches nothing
            logger.info("lessons: non-JSON distillation output — skipped")
            return 0
        if not isinstance(items, list):
            return 0
        per_repo: dict[str, list[str]] = {}
        for it in items[:6]:
            if not isinstance(it, dict):
                continue
            rid = str(it.get("repo_id") or "").strip()
            lesson = str(it.get("lesson") or "").strip()
            if rid in repo_ids and lesson:
                per_repo.setdefault(rid, []).append(lesson)
        cap = int(getattr(settings, "agentic_lessons_max", 40))
        total = 0
        for rid, lessons in per_repo.items():
            row = (db.query(RepoPathContext)
                   .filter(RepoPathContext.repo_id == rid,
                           RepoPathContext.path == LESSONS_PATH).first())
            merged, added = merge_lessons(row.content if row else "", lessons, cap)
            if not added:
                continue
            if row is None:
                row = RepoPathContext(repo_id=rid, path=LESSONS_PATH, content=merged)
                db.add(row)
            else:
                row.content = merged
            total += added
        if total:
            db.commit()
            logger.info("lessons: run=%s stored %d new lesson(s)", run_id, total)
        return total
    except Exception as e:  # noqa: BLE001 — memory is a bonus, never a failure mode
        logger.warning("lessons distillation failed (fail-open): %s", e)
        return 0
    finally:
        db.close()
