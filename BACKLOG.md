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

1. ✅ **Mid-run steering** — `POST /api/agentic/runs/{id}/steer` queues a binding
   operator message; the LIVE loop drains it at its next iteration as a user turn
   (role-fold safe) and acks durably — corrections cost seconds, not a
   park → decide → resume cycle. The committed event stream is the cross-process
   bus (`steer_request`/`steer_injected` with source_seq → exactly-once, survives
   pauses/resumes, whole exchange visible in the event feed). Scout children never
   drain; `agentic_steering_enabled` to disable.
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

## G. Working habits (third pass, 2026-11-03)

Seven mechanisms that encode three rules: **stop-safe at any moment**, **every claim
checkable**, **never leave the build broken**. Code labels are `W1`…`W7` — section F's
items carry the `G1`…`G7` labels in code, so this batch takes its own prefix rather than
colliding with them. Tests: `backend/tests/agents/test_working_habits.py`.

1. ✅ **Budget visibility** — the token hard cap is one of the three real stop conditions,
   and it lands as a soft stop *between turns*: whatever is half-finished on disk at that
   moment is what the next person inherits. The loop's budget guard already held the only
   authoritative spend-vs-cap reading in the system (the cost-weighted ledger total it
   stops on); it now hands that reading to the MODEL as a line in the pinned GROUND TRUTH
   block — "~N% used (X of Y cost-weighted, harness-measured); the run is STOPPED at
   100%" — plus sequencing advice that flips at 70% from "land a slice that compiles,
   verify it, then open the next" to "STOP opening new work; finish and verify what is
   already edited". Harness-measured, never a model claim, and read from the SAME query
   the hard stop uses, so the number the agent sees can't disagree with the number that
   kills it. Re-pinned per 10% bucket (`_BUDGET_PIN_STEP`), mirroring `_sync_fact_sheet`'s
   rev-gating — a re-pin rewrites `messages[0]` and costs a prompt-cache re-read, so a
   flat spend doesn't pay it every turn. The reading is taken BEFORE the stop check, so
   the brief in the persisted transcript is honest even on the turn the cap trips. Inert
   when no cap is configured. `RunContext.budget_{spent,cap,used_pct}` +
   `_budget_state_line` + `_sync_budget_state`.
2. ✅ **Ran-before-done nudge (deterministic)** — "I made the change and it compiles" is
   the one claim the model can always make, often sincerely, and never has to back. So
   the harness counts executions instead of reading the prose: `RunContext.command_runs`
   / `verify_runs` (+ the `executions` property) increment on EXECUTION, so a
   non-allowlisted command or a nothing-to-verify call scores zero — the counter means
   "something really ran". When the code agent declares done with file edits on disk and
   `executions == 0`, the loop spends ONE turn asking for the observed result: "you edited
   N file(s) and called run_command / verify_change ZERO times … run the build NOW and
   QUOTE the output you actually saw — the exit status and any error lines". Checked
   before the acceptance-predicate nudge (a predicate gap argued from an unbuilt tree is
   noise either way) and on its own one-shot budget, so a never-run build can't be
   silently traded away for a predicate nudge. Code phase only; `ran_nothing_nudge` event.
   Advisory by design — the agent may answer and finish — so the count travels out of the
   loop (`RuntimeResult.executions` → `ChangeSet.executions` → `art["executions"]`) and
   `_phase_code` emits `ran_nothing` when the phase ends with disk edits and zero runs:
   "the verification gate below is the FIRST execution of this change". That makes an
   ignored nudge visible, and a `needs_fix` a minute later a predictable consequence
   rather than a surprise.
3. ✅ **Last-green pointer** — a green verify is the most valuable fact a run produces and
   was the most perishable: it lived in an event payload and a boolean
   (`handoff_json["verified"]`), neither of which says WHICH round's work was green. A
   later round then regresses the build and nobody can name the state to go back to — the
   per-round rewind machinery (F6) existed with no pointer into it. `_phase_verify` now
   stamps `handoff_json["last_green_ckpt"]` = `{label, at, verify_attempt, files, note}`
   on every `verified` verdict (intermediate greens and on-demand re-verifies included,
   not just the final one), emits a `last_green` event, and `_run_view` surfaces it.
   `_latest_round_ckpt` picks the label numerically so `r10` doesn't rank below `r2`.
   The semantics are spelled out in the record itself because getting them subtly wrong
   is worse than having nothing: checkpoints are taken BEFORE each round, so green is the
   tree AFTER the round that owns `label`, and returning here after a later regression
   means reverting to the checkpoint taken BEFORE that later round — reverting *to*
   `label` would undo the very round that made it green. `label` is null (with the reason
   in `note`) when the run took no checkpoints — Phase A never rounds and checkpointing
   is fail-open — so a rewind target is never invented. Fail-open: a bookkeeping miss
   can't fail a green build.
4. ✅ **Lessons from FAILED runs** — F2's distillation fired only at COMPLETED, which is
   backwards about where the teaching material is: a completed run mostly confirms what
   already worked, while a run that died on the fourth build failure or a toolchain
   mismatch holds the lesson nobody has written down. Factored into one shared
   `_distill_lessons(db, run_id, outcome=…)` (fail-open, isolated session, 90s timeout)
   and called at terminal FAILED as well: `drive_run`'s exit — the single choke point
   every in-`_step` `mark_terminal(FAILED)` falls through to, placed AFTER the lease
   release so a bounded LLM call never blocks re-drive — plus both push-failure paths
   (preflight/base-drift and the push itself). Re-entrant: lessons dedupe on normalized
   text, so a recovery sweep re-driving a FAILED run adds nothing twice. Deliberately
   NOT called on the worker dependency-preflight failure (no phase ran, no material) or
   the lease-lost returns (that row belongs to another driver). The event carries
   `outcome` and says "learned from a FAILED run" so an operator can see the provenance.
   `_material` now LEADS with how the run died (`error_code` + `error`, 600 chars) and
   tells the distiller to generalise it only if the cause is a property of the repos or
   toolchain — without that, a failed run was distilled from "build errors seen (later
   fixed)" and the one failure that actually stopped it was the only thing missing.
5. ✅ **Stop report (deterministic, no LLM)** — when the code phase stops SHORT (token
   budget, continuation budget, two no-progress rounds) its edits stay on disk and travel
   on to verify/review, and nobody could answer the only question that matters next: how
   much of this is finished, and what of it can I trust? The evidence existed, scattered
   across the ratified plan, the disk diff, the last verification payload, the green
   pointer and the fact sheet — so the next reader reassembled it by hand from the event
   log, or guessed. `_stop_report` assembles it from HARNESS records only, never the
   agent's closing prose: planned files (ratified plan via `plan_files.plan_file_entries`,
   falling back to the agent's own `submit_plan`, then an explicit "none") vs files
   touched on disk, the gap BOTH ways (`planned_not_touched` — the actionable one — and
   `touched_not_planned`), last verify verdict, last-green pointer (W3), fact count and
   execution count (W2). Persisted at `handoff_json["stop_report"]`, emitted as
   `stop_report`, surfaced in `_run_view`. Honesty properties: `never_built` is a distinct
   state from `needs_fix` ("NEVER BUILT — trust nothing here until it is verified"), "NO
   green build was ever reached" is said out loud, file lists cap at 40 with the exact
   totals kept and the truncation NAMED, paths match by basename (the same coarse matching
   the review floor uses, since plans name full paths and the diff is repo-relative), and
   an unknown stop reason degrades to the raw token rather than vanishing. Fail-open.
6. ✅ **Green-state preservation** — verified-but-unpushed work lived in exactly ONE
   place: the on-disk clone. The hourly workspace GC collects it, and a human-approved,
   build-verified change was then destroyed by a cache eviction — `reverify_run`
   answered "expired … re-run the change", and a push whose tree was collected during
   the deferred-push gap failed its content preflight reported as "base SHA drift",
   which it wasn't. Now every GREEN verify calls `_persist_green_change_set`: the whole
   disk change-set (paths + CONTENT, deletes recorded without content) goes onto
   `handoff_json["green_change_set"]` with the per-repo base SHA and feature branch —
   the same context-as-asset channel Phase A already uses for approved XSDs. Ordered
   by (repo, path) so the same tree yields the same snapshot and omissions are
   reproducible. `_rematerialize_green` rebuilds a lost tree (re-clone → recreate the
   branch → scrub the remote token → `reset_to_base` to the RECORDED base, not the
   drifted branch head → materialize files → re-apply deletions a fresh clone would
   restore), mirroring `_adopt_parent_workspace`'s GC-recovery path. Wired into both
   `reverify_run` expired paths and into `push_run` before the content preflight.
   The safety rule is absolute: it REFUSES rather than rebuild a tree it cannot
   faithfully reproduce — nothing saved, snapshot marked incomplete, a short write, or
   an un-appliable deletion each return a precise reason, because landing a subset of a
   verified change is far worse than being told to re-run. `agentic_green_snapshot_max_bytes`
   (default 4 MB, 0 disables) is a safety limit, not a quality dial: an oversized
   change-set is stored `complete: False` with the omitted paths and their sizes NAMED
   and the knob named in the event, and the rebuild then refuses it. `_run_view` exposes
   a `green_snapshot` header with the `ops` payload STRIPPED (megabytes of file content
   have no business in a status response). Fail-open: a snapshot miss never fails a
   green build, and a rebuild never raises into a push.

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
