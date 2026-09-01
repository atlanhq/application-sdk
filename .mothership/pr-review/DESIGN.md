# SDK Review — Design rationale (maintainers only)

**The agent never reads this file.** It is not in Phase 0 step 6's read list
and must not be added to it. ORCHESTRATION.md is the agent-facing contract:
every rule, command, table, template and marker — and nothing else. The *why*
behind each rule lives here, because rationale in the playbook is paid for at
runtime on every review, on every turn after the read, and the 2026-09-01
measurements put that price at 75-90s per turn once context accumulates.

If you change a rule in ORCHESTRATION.md, record the reasoning here, not
there. If a rule looks arbitrary, this file says which incident made it.

## Why the playbook is terse

Three rounds of measured trimming (FND-1232):

1. Six conditional blocks moved to `sections/` behind lane/scope gates
   (~8.0K tokens off a conformance review).
2. Satellite reads collapsed: `review-policy.md` merged into `retro-log.md`
   (one do-not-flag list, as CLAUDE.md always claimed); `review.yaml`
   delisted (paraphrase of the rubric + CLAUDE.md).
3. This file: rationale out of agent context. External calibration:
   PR-Agent's whole rendered `/review` prompt is ~1,428 tokens.

## Incident index — what taught each rule

- **Lane marker (`LANE: sdk-loop`), Runtime**: an agent spent a paid turn on
  `ls -la /workspace/application-sdk || echo NO` inferring its lane; wrong
  inferences walk the other lane's steps and eat 403s. The marker string is
  pinned by `test_the_lane_marker_matches_the_playbook_contract` — two files,
  one string; do not loosen the test.
- **"Do not warm dependencies" (Phase 0 step 1)**: `uv sync --all-extras` sat
  here for months buying nothing — this playbook never runs pytest or
  pre-commit (step 9) — and competed with the review for I/O on every run.
  `pr-resolve` and `sdk-evolution` DO warm deps; they run tests.
- **Budget clock (step 1b, Time Budgets)**: before the clock existed the
  budget rules were unevaluable and never fired; a Small-tier PR (15-min hard
  stop) ran 62 minutes. The heredoc-at-column-0 warning: an indented
  terminator hangs the shell waiting for input.
- **Glob/grep over `.mothership/**` (step 6)**: ripgrep skips
  dot-directories, so `Glob ".mothership/pr-review/agents/*.md"` returns 0
  matches. Two measured runs burned a turn on that; worse, an agent that
  greps the reference rules for prior art, gets nothing, and concludes there
  is none raises a finding the rules already answer.
- **references/*.md deferred (§6d)**: ~125 KB, the single largest input.
  Reading it up front loaded ALL of it for EVERY review; a `minor` PR
  dispatches only `correctness` and paid for nine files to use two. Ownership
  derives from each agent's Domain Tags (`[SEC]` → security-rules, …); a file
  owned by two agents is read by both — separate contexts, sharing costs
  nothing. §1b-toolkit reads `toolkit-consumer-registry.md` directly because
  it needs the registry before any agent is dispatched.
- **Single mode (step 7)**: `COMMENTER_INTENT` is ignored because parsing
  free-form commands out of PR comments is an injection surface — there are
  deliberately no auto-fix/stop/challenge/override/focus modes.
- **CONFLICTING stays inline (step 8)**: an early draft of the sections split
  filed all of step 8 behind a "sandbox only" pointer. `NEEDS_REBASE` is a
  terminal verdict on the LOOP lane (`VERDICTS_TERMINAL`,
  sdk_loop_common.py); gated behind that pointer, a conflicted PR on
  @sdk-loop would draw ordinary findings and the loop would spend rounds on a
  branch no resolve phase can move. Pinned by
  `test_the_conflicting_rule_stays_in_the_router`.
- **`update-branch` retired (step 8)**: step 8 told the sandbox to merge base
  into a BEHIND branch while Runtime stated the review never writes on either
  lane. Both sentences were live, so the exception is gone rather than
  documented — BEHIND is now report-never-update on both lanes, the
  branch-freshness section is deleted, and the `write-branch` capability went
  with its only consumer (an ungated capability left in the matrix invites the
  next step to gate on it again). `sdk_loop_prep.py` holds write scope and
  still refuses, for the reason that settles it on either lane: merging base
  into someone's PR is a change to their branch they did not ask for, and it
  is not needed to review — the review reads the diff against base, which is
  well-defined whether or not base has moved. FND-1185 had already moved
  branch duty to prep on @sdk-loop; the sandbox had no better claim to it.
  Pinned by `test_the_review_never_updates_a_behind_branch`, which asserts
  absence of the CALL, not of the word: step 8 names `update-branch` in order
  to forbid it.
- **"Do not read CI" (step 9)**: the review cannot act on a check, CI legs
  routinely finish AFTER a review posts (so a reviewer-side snapshot was a
  stale fact next to a verdict it could not influence), and
  `sdk-review-downgrade-on-ci-failure.yml` enforces CI event-driven — the
  only race-free way. Under @sdk-loop, prep owns branch/check state.
- **Scope handed to the loop lane (step 11)**: the harness computes
  `review_scope` in Python (same file-list arithmetic); re-deriving it spends
  a turn to reach the answer already in hand.
- **Context ceiling binds the inline reviewer (§1c)**: written "per agent
  call" when every review fanned out; on 12 of 14 measured runs nothing was
  dispatched, so the only cap governed a path those reviews never took.
  Kept at 100K, not the model window: turn latency climbs ~10s → 75-90s by
  turn 12 as context grows, and grok-4.6 doubles its rate above 200K
  context. PR-Agent caps at 32K citing degradation; ours is looser
  deliberately — lowering it changes what the review sees and needs a trial.
- **Toolkit consumer setup gated on lane (1b-toolkit)**: run 33500595871
  (PR #3594) followed it into cloning five private repos on @sdk-loop; the
  scoped App token cannot, all five died with `could not read Username`, and
  the idle watchdog killed the phase with no verdict. Scope said "toolkit"
  but feasibility is decided by the credential — a lane property.
- **Last decision point before dispatch (§2a)**: a dispatch cannot be
  interrupted; 43 minutes elapsed inside a single dispatch against a 15-min
  hard stop, and the budget's `OVER HARD STOP` printed to nobody who could
  act. Hence: measure BEFORE dispatching, degrade by percentage.
- **Class sweep (§2d)**: a single revert-scope bug cost five review rounds
  because each round fixed the one reported instance, never the class.
  Reviewer-only: the conformance remediation loop's per-site independence is
  a feature — clustering fixes there trades robustness for a round-count
  problem that loop doesn't have. `class:` stays prose-only because the
  Phase 3a schema 422s on unknown fields.
- **Nit convergence (§2e′) and the strict verdict (§2h)**: the resolver loops
  until `### Findings` is empty, nits included. A reviewer that mines fresh
  pre-existing nits each pass — or lists observations with no action — makes
  that loop non-terminating and withholds approval indefinitely. Approving
  over an open nit made the reviewer the looser bar of the two and left the
  resolver working on a stamped PR. Downgrading an Important to a Nit no
  longer buys an approval, so an accepted finding is dropped with a reason,
  not demoted.
- **Toolkit inline bodies staged to files (3b, 3f)**: the redaction gate
  scans staged files; a body posted from memory bypasses it. The gate exempts
  hex control markers because it once rewrote `<!-- REVIEWED_HEAD -->` to
  `[private sha]`, sdk_review_approve.py read "no marker", skipped every
  label and the approval, and exited 0 — a green run on an unapproved PR.
- **Summary posted LAST (3f)**: it is the completion signal —
  `sdk-review-approve-on-verdict.yml` fires on it, the soft-success check
  treats it as "delivered", the §6b replay guard reads its footer. Posted
  first, all three read a partial submission as a complete one.
- **ANSWERS_TRIGGER (3e, 3f)**: two reviews can be outstanding on one PR
  (sandbox runs up to 2h; resolver waits 40 min per round; humans re-tag
  mid-review), so a verdict's timestamp cannot say which request it answers.
  The resolver's push guard reads the marker; an EMPTY marker is worse than
  none (reads as "names a round, not yours" and holds the push for the full
  stale window) — hence stamp-then-delete-if-blank.
- **REVIEWED_HEAD sed net (3f)**: repairs only the literal `<HEAD_SHA>`
  placeholder. It runs after the redaction gate on purpose — a marker damaged
  by the gate no longer matches the pattern, which is why the gate exempts
  markers rather than relying on this repair.
- **Model names in the summary footer**: filled from the models that actually
  ran; a stale hardcoded name erodes trust in everything else the summary
  claims.
