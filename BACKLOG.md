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
6. ✅ Legacy `code_change.py` defused in place (the flow stays — the Phase-B console
   UI still drives /phase-b): the compressor's SKIP list literally contained
   "compliance"/"regulatory" — the exact dropped-section bug, living on. Now a
   MANDATORY tier (the agentic seed regex) outranks both lists, dropped sections
   are NAMED in the marker, and the path logs a once-per-process deprecation
   pointing at the agentic runs. Full deletion waits on the console UI migration.
7. ✅ Seed-budget demotion is a DIRECTIVE: demoted compliance/intent sections are
   listed under "you MUST read_doc EACH before you finish; a requirement you never
   read is not implemented".
8. ✅ Verified already present: `grep` (cap + "N more matches — narrow"), `glob`
   (500 + same note), `code_search_semantic` (k≤20, 240-char snippets). No code
   change needed — the audit item was stale.
9. ✅ Reference-ranked module index, both halves: blast-radius modules float to
   the top with a "⚑" marker at render time, AND inbound-dependency counts from
   the index-time `depends_on` edges annotate load-bearing modules ("← N
   module(s) depend on this") with budget drops taking the LEAST-referenced
   modules first (named, never silent). Honest data throughout — no guessed
   ranks.
10. ✅ Residuals closed: identical ranged re-reads are stubbed (any different
    range serves — the refetch escape hatch stays open; external changes and
    edits invalidate; compaction clears). Console-UI migration VERIFIED already
    done: PhaseB.jsx drives Code/Review/Git through the agentic run for
    admin/tech_lead ("no legacy Generate Code CTA"); the legacy WS remains a
    deliberate role-gated fallback with a backend deprecation warning.
    Operator templates shipped: `knowledge_base/agent_patterns.md.example`
    (curation recipe + format) and `backend/.env.example` documents
    AGENTIC_REVIEWER_MODEL + keep-unset guidance for the retired skip lists.

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

1. ✅ Self-review made loud: when the reviewer (run_review or the goal-verifier
   panel) resolves to the AUTHOR'S model, a warning names the blind spot and the
   fix (`AGENTIC_REVIEWER_MODEL`). Which model id to use stays a deployment/env
   decision — set it per environment to what the gateway actually carries.
2. ✅ Reviewer-mode contradiction resolved: config documents `legacy` as the
   default (slipstream: one review pass; `goal_verifier` = opt-in heavier panel),
   and the orchestrator's getattr fallback now matches ("legacy", was
   "goal_verifier" — a config-skewed env silently ran the panel).
3. ✅ Dead adversarial second-pass reviewer DELETED (module + tests + inert
   `adversarial_reviewer_model` setting + router entry) — it had zero callers and
   its existence made docs claim a layer that never ran.
4. ✅ `unverified` completions are unmissable: `verify_unverified` event ("🟠 NEVER
   BUILT — … run CI or Re-verify before approving") + persisted
   `handoff_json["unverified"]` so the flag survives event scroll.
5. ✅ TSD-declared test assertions FORCE feature-test execution for that change
   (`force_feature_tests` threaded through the verifier), independent of the
   blanket `agentic_run_feature_tests` flag.
6. ✅ Context-degradation visibility: failed grounding sources (impact graph —
   both engines, file census, ratified plan load) accumulate into
   `ContextPack.degraded` and render as a "⚠ CONTEXT DEGRADED" prompt block —
   "absent, not empty; verify with tools". Remaining fail-opens (gate suspects,
   predicates, PII redaction) are reviewer-side and stay logged-only for now.

## D. Planning polish

1. ✅ `submit_plan` schema validation with bounce-back errors; `delete` legalized.
2. ✅ `submit_plan` carries a worked example in its description, and modify/delete
   entries may cite `evidence:[{file,line}]` — verified against the read-tracking
   ground truth (`_verify_evidence`) and reported back ("M/N entries grounded"),
   advisory by design (the deterministic gates do the enforcing).
3. ✅ Dormant-planner fate decided: `code_planner.py` DELETED (zero production
   callers — the live plan path is `submit_plan` + `code_plan_schema`);
   `diff_stats_gate` turned out to be WIRED (orchestrator completion round) and
   stays; `code_plan_schema` stays as the live validator.
4. ✅ `agent_patterns.md` absence made loud: a once-per-process warning names the
   file, the consequence (no house patterns reach the code agent) and the curation
   recipe. Content curation itself remains operator work by design — patterns are
   per-ecosystem facts.
5. ✅ Covered by C6 (context-degradation block: impact graph, census, plan load).

## E. Measurement (makes everything above provable)

1. ✅ Outcome metrics: `app/services/agentic_metrics.py` aggregates runs + events
   into success rate (terminal runs), final verify status per run, NEVER-BUILT
   completions, review rounds, verify attempts (≈ rounds-to-green), per-gate fire
   counts (the shadow evidence for hardening/deleting a gate) and loop health.
   Exposed at `GET /api/admin/agentic/metrics` (admin-gated; ?days=&limit=).
2. ✅ Golden cases added: `case_003` (XSD doc — rides the SAME B2 compile gate
   production uses, so a non-compiling golden candidate fails structurally) and
   `plans/plan_case_001` (code plan — judged by the live `code_plan_schema` +
   planned-file coverage vs the golden; the 13-dropped-files bug class as a score).
3. ✅ `CODE_TO_REVIEW` / `REVIEW_TO_BUILD` stubs DELETED with a pointer: Phase B
   measurement lives in `agentic_metrics`, not the docgen checkpoint framework.

## F. Claude-parity gaps (second audit, 2026-10-30)

1. ⬜ **Mid-run steering** — the one deliberately left open: no channel to talk to a
   RUNNING agent (corrections cost a park/decide/resume cycle). Build: a `steer`
   endpoint queuing an operator message, drained at the next loop iteration as a
   binding user turn; also lets ask_decision answers arrive without parking.
2. ✅ Cross-run lessons memory: at COMPLETED, one bounded UTILITY-tier LLM call
   distills the run's durable repo-general gotchas into `AGENT_LESSONS.md` rows
   (RepoPathContext — no migration), deduped + capped FIFO, operator-editable;
   injected into future runs' prompts as verify-before-relying hints. The only
   mechanism here that makes the system better every week on its own.
3. ✅ Round-aware verify speed: loop-back rounds build INCREMENTALLY (seconds, not
   a from-scratch reactor); a green incremental round is SEALED by one clean pass
   so stale-target/ masking stays impossible. Iterations are accuracy.
4. ✅ Contained scratch runner (`scratch_run`, DEFAULT OFF): short python3 scripts
   in a rlimited tempdir for hypothesis-testing diagnosis, repos exposed via
   repos.json read-intent. Off because it changes the exec posture — flip
   `agentic_scratch_enabled` where the deployment accepts it.
5. ✅ Operator tool hooks: executables around every tool call (JSON on stdin);
   pre-hook exit 2 blocks with its stdout as the model-visible error; fail-open.
   Deployment policy without forking the harness.
6. ✅ Per-round rewind: a checkpoint of every repo worktree before each code round
   (`refs/atom/ckpt/*` — invisible to the MR, worktree/index untouched) +
   `POST /api/agentic/runs/{id}/revert-round/{label}` restoring content and
   deleting post-checkpoint files, lease-guarded.
7. ✅ Metric blind spots closed: `scout_done` + `reads_superseded` events;
   metrics now report scout adoption and `context_reclaimed_chars` (compaction +
   supersede-eviction) — the context-discipline work is measurable.

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
