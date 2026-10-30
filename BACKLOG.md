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
4. 🔶 **Subagent isolation** — phases are isolated; ⬜ add a read-only **scout
   subagent** inside the code phase: exploration happens in the child, the editor
   context carries only the brief.
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

1. ⬜ Inject base/involved XSDs into `stream_xsd_turn` — the generator is told to
   preserve schemas it is never shown (reuse `build_involved_xsd_bundle`).
2. ⬜ `xmlschema` compile of every generated block + bounded repair loop feeding
   compiler errors back; also lift `retry_allowed=False` on the XSD checkpoint.
3. ⬜ Kill hard truncations: `tech_spec[:4000]`, `brd[:2000]` (xsd.py),
   `source_xsd[:8000]` (TSD writer) → section-aware selection + log when clipped.
4. ⬜ Structured REQUIRED/NOT-REQUIRED decision (replace `"**REQUIRED**" in text`).
5. ⬜ `xsd_bundle` filename attribution: structured per-file output instead of the
   last-`.xsd`-token-in-600-chars regex — mislabeled blocks ship to partners.
6. ⬜ Promote the over-escaped `xs:pattern` facet warning to blocking once B2 exists.
7. ⬜ Minor: `recover=True` lxml parsing can yield partial element indexes →
   spurious NEW/DEPRECATED diffs (flag loudly).
8. ⬜ Minor: `xsd_namespace` reuse misses undeclared namespace spelling variants.
9. ⬜ Minor: `schema_amendment.apply` exact-match anchors — moved text surfaces as
   `failed`; consider the edit-ladder used by `agentic_edit`.

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
