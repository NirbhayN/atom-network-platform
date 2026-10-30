# Agentic codegen — accuracy & context backlog

Master list from the 2026-10-30 accuracy audit + slipstream work. Status:
✅ done · 🔶 partial · ⬜ open. Quick wins marked ⚡.

## A. Claude-style context discipline (the five pillars)

1. ✅ **Pull, don't pre-stuff** — context pack ships indexes only; code is pulled
   via tools. Remaining: A6, A7 below.
2. ✅ **Tool-result eviction** — threshold eviction to stubs, plus supersede-aware
   eviction: the moment `edit_file` rewrites a file, every older read of it in
   history is stubbed immediately (`_evict_superseded_reads`, protocol-safe,
   respects the eviction floor; staged/skipped schema writes don't trigger it).
3. ✅ **Auto-compact with structured state** — verified stronger than audited: the
   brief (msg[0], carrying the plan) is protected by BOTH `trim_messages_to_fit`
   and `_compact_messages`; system blocks are never trimmed at dispatch; the
   summary call already has a two-step overflow ladder. Invariants now pinned by
   tests. (Audit note: `pack_within_budget` — the "plan could drop" risk — turned
   out to be UNCALLED dead code; candidate to wire into docgen or delete.)
4. ✅ **Subagent isolation** — phases are isolated, AND the code agent now has a
   `scout` tool: one broad research question runs in an isolated read-only child
   loop (own RunContext, read-only tool subset, no plan requirement, bounded
   iterations, shared per-run token budget); only the path:line brief returns —
   the child's reads never enter the editor's context. Recursion-proof (a scout
   carries no scout tool); `agentic_scout_enabled` to turn off.
5. ✅ **File-state tracking** — a repeat FULL read of an unchanged file returns a
   short "UNCHANGED since your last full read" stub (`ctx.fresh_reads`): an edit
   discards the entry, external changes re-serve content (hash check), compaction
   clears the registry so a stub never points at evicted content, and ranged
   reads are always served (the refetch escape hatch).
6. ⬜ Retire legacy `code_change.py` (pre-stuffs top-k RAG, 20KB KEEP/SKIP doc
   compression that can drop regulatory sections — the agentic path fixed this,
   the console path didn't).
7. ⬜ Seed-budget demotion: when an intent-matched doc section is demoted to a
   `read_doc` pointer, force the follow-up read (or list demoted headings loudly).
8. ⬜ Capped search results (SWE-agent style): `grep`/`code_search_semantic` return
   "N matches, top 20 — narrow the query" instead of everything.
9. ⬜ Aider-style ranked repo map: upgrade `module_notes` from flat name+summary to
   a reference-ranked symbol skeleton within the same 8KB budget.

## B. XSD generation (biggest unclaimed accuracy jump)

1. ✅ Base-schema grounding: `stream_xsd_turn`/`stream_xsd_assessment` now receive
   the CURRENT content of every involved schema (flow + spec-named files, workspace
   or KB, 40K cap via `xsd_generation_schema_context_chars`). Bonus fix found en
   route: refine turns ran entirely WITHOUT the spec/BRD (the persisted
   conversation strips turn-1's ephemeral context) — every multi-turn call is now
   re-anchored with a leading context exchange.
2. ✅ Compile gate + repair loop: every generated ```xml block must be well-formed,
   xs:schema-rooted, and build under `xmlschema` (sandboxed temp dir — sibling
   include/import resolves inside the document; external base-schema refs and
   their unknown-type fallout are not defects). Compiler errors feed up to
   `xsd_compile_repair_rounds` (2) full re-emissions; residual errors surface in
   the done payload/job result, never silently ship. `retry_allowed` flipped to
   True on the TECH_SPEC_TO_XSD checkpoint (a retry is now compiler-checked).
3. ✅ Truncations killed: spec/BRD get section-aware clipping (whole sections kept,
   omitted headings NAMED in the prompt + logged; caps 16K/8K configurable); the
   TSD writer's `source_xsd[:8000]` is now a 24K head+tail clip with a loud
   "treat the schema set as INCOMPLETE" marker.
4. ✅ Structured decision: `parse_xsd_decision` reads the assessment's `### Decision`
   section (prose like "a REQUIRED field" can no longer flip the verdict); legacy
   whole-document heuristic kept as a logged fallback.
5. ✅ Filename attribution: extraction scoped to the inter-block region (a filename
   in the previous section's prose can't claim the next block), re-emitted files
   last-body-wins, and the generation prompt now mandates the per-file heading
   because "the platform ships files by that heading".
6. ✅ Blocking schema-write guard (`_xsd_write_guard`): an edit that INTRODUCES an
   xs:pattern facet defect, or turns a well-formed .xsd/.xjb into broken XML,
   rejects BEFORE the write with the fix in the error. Pre-existing defects in
   untouched patterns stay advisory (never wedged on legacy content);
   `agentic_xsd_facet_block` to disable the facet tier.
7. ✅ Lossy-parse flagging: `XsdDiff.lossy` + `parse_is_lossy` — a diff built from a
   recovered (partial) parse is marked `parse_lossy` where consumed, so spurious
   NEW/DEPRECATED read as approximate, not fact.
8. ✅ Namespace matching: scheme+host lowercased per RFC 3986 (path stays
   case-sensitive); different hosts/schemes remain pack-declared only.
9. ✅ `schema_amendment.apply` + `describe.applicable` use the same whitespace-
   tolerant edit ladder as `edit_file` — a reformat between staging and approval
   no longer fails the replay; ambiguity/absence still refuse loudly.

## C. Phase B review/verification hygiene

1. ⬜ Point `agentic_reviewer_model` at a different model than the author
   (config/env only — the self-review blind spot is documented in llm_router).
2. ⬜ Resolve the reviewer-mode contradiction: docstring says `goal_verifier` is
   default, config default is `legacy`.
3. ⬜ Wire-or-delete the dead adversarial second-pass reviewer (zero callers).
4. ⬜ Make `unverified` (DeferredVerifier) completions loud at the approval gate.
5. ⬜ Auto-require feature tests when the TSD declares test assertions (targeted,
   not the blanket `agentic_run_feature_tests` flag).
6. ⬜ Fail-open visibility sweep: the many `noqa: BLE001` silent degradations
   (impact graph, module notes, gate suspects, predicates, PII redaction) should
   emit events + a prompt note so the agent and operator see what was lost.

## D. Planning polish

1. ✅ `submit_plan` schema validation with bounce-back errors; `delete` legalized.
2. ⬜ Few-shot example in the `submit_plan` tool description + evidence citations
   for plan file entries (extend `_verify_evidence` beyond reuse_decisions).
3. ⬜ Decide fate of dormant `code_planner.py` / `diff_stats_gate.py` ("not wired
   yet") — wire as optional pre-step or delete.
4. ⬜ Curate or auto-generate `agent_patterns.md` (repo conventions reach the code
   agent only through this often-empty operator file).
5. ⬜ Context starvation made loud: impact-files cap / KG fail-open to `[]` should
   inject a visible "blast radius unavailable — verify via symbol_graph" note +
   event (overlaps C6).

## E. Measurement (makes everything above provable)

1. ⬜ Outcome metrics aggregated from `agentic_events`: success rate,
   rounds-to-green, verify status at completion, gates fired-vs-overridden.
2. ⬜ Golden harness cases for ChangeAnalysis plans and XSD generation
   (currently docgen-only, scoring-only).
3. ⬜ Implement or delete the `CODE_TO_REVIEW` / `REVIEW_TO_BUILD` checkpoint
   enum stubs (defined, contract-less, unwired).

## Done (commits e8b7ddf, c39a6c9, a42434e)

- Per-module Java detection (legacy `1.8` ≡ `8`, property-aware, nested poms),
  per-step JDK selection, mixed-major reactor rescue (rescue runs BEFORE
  soft-fail), `agentic_soft_fail_modules`/skip defaults emptied, Temurin 8 in the
  backend image.
- Slipstream posture as default config: approach gate off, analytical gates
  advise-only, behavioral gaps advisory from round 1, loops budget-bound
  (64 continuations / 40 attempts), hard stops = build verdict + token budget +
  human approval.
- `submit_plan` validated at the door; 30+ scenario tests incl. pinned defaults.
- Context discipline (pillars A2/A3/A5): supersede-aware eviction after edits,
  unchanged-read stubs with compaction-safe invalidation, and compaction/trim
  invariants (brief + assistant reasoning never evicted) pinned by tests.
- Scout subagent (pillar A4): `scout` tool in the code phase — isolated read-only
  child loop answers one research question; only the brief returns.
- XSD section complete (B1–B9): base-schema grounding + compile/repair loop,
  section-aware prompt clipping, structured decision parse, scoped filename
  attribution, blocking schema-write guard, lossy-parse flags, RFC-correct
  namespace matching, edit-ladder amendment replay.
