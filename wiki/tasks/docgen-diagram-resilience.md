# Task Sheet — Docgen diagrams: silent loss when the LLM provider is down; no PlantUML error-retry

**Date raised:** 2026-10-13
**Status:** PROPOSED
**Severity:** Medium — generated BRD/TSD documents ship with missing or placeholder flow/sequence diagrams and nothing in the job status says so; the cause is only discoverable by grepping worker stderr.

---

## Problem (confirmed from production logs, host CHSRBMGENAI002, `logs/a2a-platform/app-stderr.log`)

Diagrams were reported "not there" after a test run. Investigation showed **three distinct weaknesses**, none of them a renderer-installation problem (PlantUML is installed and working on the host; mermaid is not needed — `DOCGEN_DIAGRAM_ENGINE` defaults to `plantuml`):

1. **2026-10-09 10:49 — total diagram loss during an LLM provider outage.**
   `circuit breaker OPEN for provider='ainxt' … failing fast without a network call` hit **both** LLM steps of `_generate_single_diagram` (`backend/app/docgen/agents/pipeline.py`): the PlantUML source generation *and* the JSON-spec Pillow fallback (`LLM JSON diagram spec failed … using empty`). With an empty spec the Pillow renderer draws a meaningless generic placeholder (default `User/System` or `Start→Process→End`). The pipeline then **completed the document normally** — no degraded status, no warning surfaced to the job/UI. The breaker (`backend/app/core/resilience.py`) opens after 5 consecutive provider failures and cools down in ~30 s (`llm_circuit_breaker_cooldown_s`), so a single short outage permanently costs that run its diagrams even though a 30 s retry would have recovered them.

2. **2026-10-05 / 2026-10-08 — recurring PlantUML syntax failures with no fix-and-retry.**
   `PlantUML exited 200: ERROR … This swimlane must be defined at the start of the diagram. Some diagram description contains errors.` The LLM emitted invalid activity/swimlane PlantUML. The Mermaid path already feeds the renderer's parse error back to the LLM for one corrective retry (`pipeline.py` ~L1033–1052); the PlantUML path has no equivalent — `generate_plantuml_diagram` (`backend/app/docgen/tools/diagram_generator.py`) logs stderr and returns `None`, discarding the error text. The run silently downgrades to the plainer Pillow fallback.

3. **Renderer availability is invisible at `--loglevel=info`.**
   "plantuml.jar not found" / "mmdc not found" are logged at **DEBUG** only. On a host missing a renderer, the permanent downgrade to Pillow output never appears in the pm2 worker logs. (Not the cause of this incident, but it is why diagnosing it required reading code instead of logs — "how should we know".)

---

## Tasks

### T1 — Surface degraded diagram generation in job status (+ one post-cooldown retry)

**Files:** `backend/app/docgen/agents/pipeline.py` (`_generate_single_diagram`, `generate_diagrams`), job/status plumbing that the UI reads.

- In `_generate_single_diagram`, catch `CircuitOpenError` (`app/core/resilience.py`) distinctly from other LLM failures and propagate a machine-readable reason (e.g. `("llm_unavailable", provider)`) instead of silently falling through to the empty-spec Pillow render.
- In `generate_diagrams`, when one or more diagrams failed for `llm_unavailable`: wait out the breaker cooldown once (`llm_circuit_breaker_cooldown_s`, default 30 s, cap the wait) and retry just the failed specs. The breaker half-opens after cooldown, so one retry recovers a transient outage.
- If diagrams are still missing/placeholder after the retry, complete the run but record it as **degraded**: add e.g. `state["diagram_warnings"] = ["diagram_3_7db324: skipped — LLM provider 'ainxt' unavailable", …]`, persist alongside `generated_diagrams.json`, and surface in the job status/response the frontend shows. A document with missing figures must say so somewhere the operator actually looks.
- Do **not** render the empty-spec placeholder when the spec is empty because the LLM was unreachable — a missing figure with a warning beats a wrong generic figure.

### T2 — PlantUML fix-and-retry with the real renderer error (mirror the Mermaid path)

**Files:** `backend/app/docgen/tools/diagram_generator.py`, `backend/app/docgen/agents/pipeline.py`.

- Add `render_plantuml(source, output_path) -> tuple[Optional[str], str]` returning `(path_or_None, stderr_text)` — same shape as `render_mermaid`. Keep `generate_plantuml_diagram` as a thin backward-compatible wrapper (same pattern already used for mermaid at the top of the module).
- In `_generate_single_diagram`'s PlantUML step: on render failure, make **one** corrective LLM call feeding back the actual PlantUML stderr (mirror the Mermaid retry at `pipeline.py` ~L1036–1052), then re-render before falling through to the Pillow path.
- Extend `plantuml_system` prompt rules with the known failure mode from production: for ACTIVITY diagrams with swimlanes, every `|Lane|` must be declared/entered before the first activity in it, with a short valid example — the same technique that cut Mermaid sequence failures (`_mmd_hint`).

### T3 — One-time renderer capability log at WARNING

**Files:** `backend/app/docgen/tools/diagram_generator.py` (plus first-use call site).

- On first diagram render per process (module-level `once` flag), log at **WARNING** when running Pillow-only, and at INFO the full capability map: `mmdc: yes/no, mermaid docker image: yes/no, plantuml.jar+java: yes/no → effective engine: plantuml`. Makes the silent-downgrade state visible in standard pm2 `--loglevel=info` worker logs.

---

## Acceptance

- A run executed while the LLM breaker is open ends with either recovered diagrams (post-cooldown retry) or an explicit degraded status naming the skipped diagrams and the provider — never a silently figure-less document.
- An LLM emitting the Oct-5/8 swimlane error now gets the stderr back and a second chance; only a second consecutive failure falls back to Pillow.
- `tests/docgen/test_diagram_source.py` extended: breaker-open path (no placeholder, warning recorded), PlantUML retry path (bad source → error fed back → fixed source renders), capability log emitted once.
- Worker logs on a Pillow-only host show the WARNING capability line at startup/first render.

## Diagnosis cheatsheet (ops)

```bash
# the diagram-level symptoms
grep -anE "diagram|plantuml|mmdc" logs/a2a-platform/app-stderr.log | tail -50
# the root cause that trips the breaker (look just BEFORE the 'circuit breaker OPEN' lines)
grep -anE "LLM_CIRCUIT|ainxt" logs/a2a-platform/app-stderr.log | tail -50
```

Breaker self-heals ~30 s after the provider recovers; re-running the generation then restores diagrams with no restart needed.
