# SDK Review — Orchestration Playbook

Follow these phases EXACTLY. Do not skip phases. Do not reorder.
Print `[Phase N complete]` after each phase, followed by `bash /tmp/budget.sh`
(see Time Budgets — the elapsed number is measured, never estimated).

## Conditional sections — read only what your lane and scope select

Six blocks of this playbook live under `sections/` instead of inline, and each
is pointed to from the step that owns it, with the condition for reading it.
**Do not read a section whose condition you do not meet.**

| `sections/…` | Read it when |
|---|---|
| `sandbox-guards.md` | mothership sandbox lane |
| `adversarial-wave-2.md` | mothership sandbox lane |
| `prior-review-and-delta.md` | your lane computes its own prior review + delta |
| `branch-freshness.md` | your lane holds a write scope |
| `scope-classification.md` | your lane must derive `review_scope` itself |
| `toolkit-consumer-setup.md` | `review_scope` is `contract-toolkit` or `mixed-sdk-toolkit` |

This is not tidying. Everything you read stays in context for every turn that
follows, and measured turn latency on the review model climbs from ~10s early
in a phase to 75-90s by turn 12 as that context grows — so a section you load
and never use is charged to every remaining turn of the review. On a
conformance-only `@sdk-loop` review all six conditions are false, which is
~7.7K tokens not carried. Nothing has been deleted: each file holds its
original text verbatim, and a test asserts every one of them is still reachable
from a pointer here.

## Runtime

Two lanes run this playbook, and they differ in what the SURROUNDING system
already guarantees. Everything about what a good review IS — routing, agents,
severity, the verdict — is identical. Only the bookkeeping differs, and doing
a lane's bookkeeping twice is not free: each step is a model round trip, and
several of them cannot even succeed on the wrong lane.

<!-- CONTRACT: the literal string `LANE: sdk-loop` below is emitted by
     review_prompt() in .github/scripts/sdk_loop_phase.py. Two files, one
     string — exactly the shape that rots silently, because a playbook that
     waits for a line nobody sends does not error, it just quietly runs the
     wrong lane's steps and eats the 403s. Renaming it on either side without
     the other breaks lane detection with no failing test and no log line
     saying so. test_the_lane_marker_matches_the_playbook_contract in
     .github/scripts/tests/test_sdk_loop.py asserts both sides agree; if you
     change this string, that test fails and tells you where the other half
     lives. Do not "fix" the test by loosening it. -->

**You are told which lane you are on. Do not work it out.** The dispatch
prompt states it: the `@sdk-loop` harness emits the line `LANE: sdk-loop`,
and its absence means the mothership sandbox. Inferring it instead — probing
for `/workspace`, reading `pwd`, checking for a runner env var — costs a turn
and can be wrong; a live transcript shows an agent spending one on
`ls -la /workspace/application-sdk … || echo "NO /workspace/application-sdk"`
before doing any review work.

This matters because the difference is not cosmetic. Several steps below
**cannot succeed** on the wrong lane: they need a write scope the `@sdk-loop`
review token does not have, and they fail with a 403 after the model has
already been paid for the turn that made the call.

| | mothership sandbox | `@sdk-loop` (GitHub Actions) |
|---|---|---|
| Working directory | `/workspace/application-sdk` | the checkout you start in |
| Duplicate triggers | A3/A4 guards | the Fence job, before any model runs |
| Replay after a dropped stream | A3 guard | n/a — one invocation per job |
| Stale / moved HEAD | A2 guard | the harness re-aims and restarts the round |
| Per-run `/tmp` hygiene | A1 guard | fresh runner every phase |
| Prior review + delta | you compute it (§6b, §6c) | handed to you in the prompt; still verify |
| Branch update when BEHIND | §8 | your token has no write scope — report, do not attempt |
| Commit status / labels / approval | the GHA layer (§3c) | the GHA layer (§3c) |

**The review never writes to the branch on either lane.** It posts a summary
comment and inline findings; it does not commit, push, run `pre-commit`, run
tests, or fix CI. On `@sdk-loop` that is enforced by the credential rather
than by this sentence: the review phase holds a token with no `contents` and
no `statuses` scope, so an attempt fails with a 403 rather than doing harm.
Do not treat such a 403 as something to work around.

## Time Budgets

Budgets scale with PR size. Determine the tier in Phase 0, then use
the corresponding column. If approaching the limit, finalize with what
you have — a partial review is better than no review.

| Phase | Small (<2K lines) | Large (2K-20K) | Massive (20K+) | What to cut if over |
|-------|-------------------|----------------|----------------|---------------------|
| Phase 0: Orient | 30s | 30s | 60s | Nothing — fast |
| Phase 1: Context | 60s | 2 min | 5 min | Skip reachability, grep-only |
| Phase 2: Review | 5 min | 10 min | 15 min | Drop to 2 agents, skip adversarial |
| Phase 3: Submit | 30s | 30s | 60s | Nothing — just a curl call |

| PR Size | Total Budget | Hard Stop |
|---------|-------------|-----------|
| Small (<2K lines) | ~10 min | 15 min |
| Large (2K-20K) | ~18 min | 25 min |
| Massive (20K+) | ~26 min | 35 min |

If the sandbox has been running past the hard stop, finalize immediately
with whatever findings you have. Post the review summary + commit
status — never exit without posting to the PR.

### Measuring the budget (MANDATORY — these numbers are not decorative)

You cannot feel elapsed wall-clock time. Every budget rule below — the
degradation priority in §2a, the "over 70% consumed" skip in §2b, and the
hard stop above — is a condition you MUST measure, not estimate. Until
this section existed there was no clock anywhere in this playbook, so
those rules were unevaluable and never fired: a Small-tier PR (15 min
hard stop) ran **62 minutes** because nothing told the agent it was over.

Phase 0 stamps the start time and writes `/tmp/budget.sh`. Run it at
**every phase boundary** and immediately before §2b:

```bash
bash /tmp/budget.sh
# [budget] elapsed 8m 12s / hard stop 15m (54%) — OK
# [budget] elapsed 11m 40s / hard stop 15m (77%) — OVER 70%: skip adversarial Wave 2
# [budget] elapsed 16m 02s / hard stop 15m (106%) — OVER HARD STOP: finalize now
```

Act on what it prints. `OVER 70%` and `OVER HARD STOP` are instructions,
not warnings. If `/tmp/budget.sh` is missing (a resumed session that
skipped Phase 0), recreate it with the Phase 0 step 1b snippet before
continuing — never proceed with an unmeasured budget.

---

## Phase 0: Orient (~30s)

The dispatch prompt passes the PR context directly. Read these values
from the prompt header (do NOT re-derive):

```
PR_NUMBER, PR_URL, REPO, HEAD_SHA, BASE_REF, HEAD_REF,
COMMENTER, COMMENT_ID, COMMENTER_INTENT
```

1. **Set working directory.** On the mothership sandbox the repo is cloned
   on the PR head ref into `/workspace/application-sdk`, so `cd` there. Under
   `@sdk-loop` you already start in the checkout — do not look for
   `/workspace`, it does not exist on a runner and probing for it costs a turn.

   Do **not** warm dependencies. This playbook never runs `pytest` or
   `pre-commit` — §9 is explicit that the review does not run them — so the
   `uv sync --all-extras` that used to sit here bought nothing and competed
   with the review for I/O on every single run. `pr-resolve` and
   `sdk-evolution` DO run them and warm deps for that reason; this playbook
   is not those.

1b. **Start the budget clock** — do this before any other work, so the
    elapsed number covers the whole run. Defaults to the Small hard stop;
    step 12 raises it once the tier is known.

Run this block **exactly as written, unindented**. The heredoc terminator
must sit at column 0 or `cat` never closes it and the shell hangs waiting
for input:

```bash
date +%s > /tmp/REVIEW_START_TS
echo 900 > /tmp/REVIEW_HARD_STOP_S   # 15 min, Small tier default

cat > /tmp/budget.sh <<'BUDGET'
START=$(cat /tmp/REVIEW_START_TS 2>/dev/null || echo 0)
CAP=$(cat /tmp/REVIEW_HARD_STOP_S 2>/dev/null || echo 900)
[ "$START" -gt 0 ] || { echo "[budget] no start stamp — rerun Phase 0 step 1b"; exit 0; }
E=$(( $(date +%s) - START ))
PCT=$(( E * 100 / CAP ))
if   [ "$PCT" -ge 100 ]; then VERDICT="OVER HARD STOP: finalize now"
elif [ "$PCT" -ge 70 ];  then VERDICT="OVER 70%: skip adversarial Wave 2"
else                          VERDICT="OK"
fi
printf '[budget] elapsed %dm %02ds / hard stop %dm (%d%%) — %s\n' \
  "$((E/60))" "$((E%60))" "$((CAP/60))" "$PCT" "$VERDICT"
BUDGET
```

2. **Auth setup** — `$GITHUB_TOKEN` is injected by mothership from its
   GitHub App installation (see snapshots/_base). Make `gh` use it:
   ```bash
   echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
   ```

3. **Fetch authoritative PR metadata** (no session/PR.md anymore):
   ```bash
   gh pr view "$PR_NUMBER" --repo "$REPO" \
     --json number,state,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid,title,body,labels \
     > /tmp/PR.json
   ```

4. **Fetch authoritative diff** (no session/DIFF.patch anymore):
   ```bash
   gh pr diff "$PR_NUMBER" --repo "$REPO" > /tmp/DIFF.patch
   ```

4b–5. **Sandbox-only run guards** — resetting `/tmp` artifacts and the
   stale-SHA bail-out. Under `@sdk-loop` neither applies: every phase gets a
   fresh runner with nothing to reset, and the harness owns HEAD movement
   (`head_state`, and the Fence job). See Appendix A. On the sandbox, run
   them.

6. **Read in-repo orchestration assets** — these are the source of truth
   for SDK review behavior. All paths are relative to the repo root:
   - `.mothership/pr-review/CLAUDE.md`
   - `.mothership/pr-review/severity-rubric.yaml` (includes severity
     calibration + confidence floors — the single source for both)
   - the brief for each agent §2a will dispatch, **by explicit path**
     (`.mothership/pr-review/agents/<name>.md`) — not all of them, and
     never via Glob. `Glob ".mothership/pr-review/agents/*.md"` returns
     **0 matches**: the tool is ripgrep-backed and ripgrep skips
     dot-directories. Two measured runs each burned a turn on that
     zero-match. The same trap applies to any grep over `.mothership/**` —
     an agent that searches the reference rules for prior art, gets nothing,
     and concludes there is none will raise a finding the rules already
     answer. That is the expensive failure; the wasted turn is the cheap one.
   - `.mothership/review-policy.md`
   - `.mothership/review.yaml`

   **`references/*.md` is NOT read here.** It is ~125 KB — over half
   everything this step would otherwise load — and it is consumed by the
   Phase 2 agents, which receive their reference rules when dispatched
   (§2a). Reading it up front pays for it twice, and pays for it at all on
   reviews where no agent ever runs. Defer it to §6d.

6b/6c. **Prior review and the re-review delta.**

    * `LANE: sdk-loop` — both are **handed to you in the dispatch prompt**: the
      prior verdict, and the incremental range as `git diff <prior>..<head>`.
      Verify them; do not recompute them.
    * mothership sandbox — **read only when:** you are on that lane; then
      follow 6b, 6c and 11b in `sections/prior-review-and-delta.md`.
6d. **Do NOT read `references/*.md`. The agents that use them read them.**

    Each Phase 2 agent now names the reference files it owns, at the top of
    its own `agents/*.md`, and reads them itself:

    | Agent | Owns |
    |---|---|
    | `correctness.md` | `security-rules`, `v3-architecture-rules`, `performance-rules` |
    | `quality.md` | `code-quality-rules`, `test-quality-rules`, `dx-rules`, `retro-log` |
    | `structure.md` | `structural-rules`, `v3-architecture-rules` |
    | `ci-config.md`, `conformance.md` | `retro-log` |
    | `toolkit-review.md` | `toolkit-consumer-registry` |

    The ownership is derived from each agent's own Domain Tags, not assigned
    by hand: `[SEC]` → security-rules, `[QUAL]` → code-quality-rules, and so
    on. A file owned by two agents is read by both — they are separate
    contexts, and sharing costs nothing.

    Why this is a real change and not tidying: these files are ~125 KB, the
    single largest input to a review, and reading them here loaded ALL of them
    for EVERY review no matter which agents ran. A `minor` PR dispatches only
    `correctness` and paid for nine files to use two. Reading them at the point
    of use also fixes an older ambiguity — §2a said each agent receives "their
    reference rules" without anywhere defining which those were, so six of the
    nine files were named by nothing at all and reached agents only because the
    orchestrator had globbed everything.

    You still read `severity-rubric.yaml`, `CLAUDE.md`, `review-policy.md` and
    `review.yaml` in step 6: those govern YOUR decisions, not an agent's.

    Exception: §1b-toolkit reads `toolkit-consumer-registry.md` directly,
    because it needs the registry before any agent is dispatched.

7. **Always run a standard review.** There is a single mode. Ignore any
   free-form text after `@sdk-review` (`COMMENTER_INTENT`) — there are no
   commands to parse (no auto-fix, stop, challenge, override, or focus
   modes). Every trigger runs the full multi-agent review for the PR's
   `review_scope`, posts findings, and exits.

   **Re-review continuity:** if `/tmp/PRIOR_REVIEW.md` is non-empty (a prior
   `<!-- SDK_REVIEW -->` summary exists — loaded in Phase 0 step 6b), this
   run is a **re-review**: prior findings + author replies are part of the
   input. Carry forward findings that are still present, label resolved ones
   explicitly, surface new ones, and downgrade ones the author successfully
   addressed in inline-comment threads. See §2e for the labeling rules.
   On a re-review with a known delta (step 6c), the *breadth* of Phases 1–2
   is cut to that delta per step 11b — the continuity rules are unchanged.


8. **Branch freshness + conflict resolution.**

    * `LANE: sdk-loop` — your token carries no write scope. **Report a behind
      or conflicted branch as a finding and attempt nothing.** A push fails
      with a 403 after the turn has already been paid for.
    * mothership sandbox — **read only when:** you are on that lane; then
      follow `sections/branch-freshness.md`.
9. **Do not read CI.** Removed, not moved: the review cannot act on a check
   either way — it holds no write scope on this lane — and
   `sdk-review-downgrade-on-ci-failure.yml` already enforces CI against the
   verdict event-driven, which is the only race-free way to do it. CI legs
   routinely finish AFTER a review posts, so a reviewer-side snapshot was
   always a stale fact reported next to a verdict it could not influence.
   Under `@sdk-loop` the prep phase owns branch and check state before the
   first review starts. Spend no turn on `gh pr checks`.

10. Read the repo's `CLAUDE.md` for project conventions.

11. **Smart agent routing.** `review_scope` decides which specialists §2a
    dispatches.

    * `LANE: sdk-loop` — **you are told your scope. Do NOT derive it.** The
      harness computed it in Python before the phase started, from the same
      file-list arithmetic, and re-running that classification spends a turn to
      reach the answer you already have.
    * mothership sandbox — **read only when:** you are on that lane; then
      follow `sections/scope-classification.md` to derive `review_scope`.

    §2a's routing table below is the authority for scope-to-agent mapping on
    both lanes, and stays here.
11b. **Re-review delta scoping** — **read only when:** `DELTA_KNOWN=1` from
    step 6c, which only the sandbox lane computes. It lives at the end of
    `sections/prior-review-and-delta.md`.
12. Check diff size for tier, and raise the budget cap to match it
    (step 1b defaulted to the Small hard stop):
    - < 2000 lines → `review_tier = "full"` → `echo 900 > /tmp/REVIEW_HARD_STOP_S`
    - 2000-20000 → `review_tier = "partitioned"` → `echo 1500 > /tmp/REVIEW_HARD_STOP_S`
    - > 20000 → `review_tier = "staged"` → `echo 2100 > /tmp/REVIEW_HARD_STOP_S`
    On a delta-scoped re-review (step 11b), tier from `DELTA_LINES`, not the
    full PR diff — the budget should match the work actually remaining.

Print: `[Phase 0 complete] PR #<N>, scope=<scope>, tier=<tier>` followed by
`bash /tmp/budget.sh`

---

## Phase 1: Context Gather (~60s)

### 1a. Holistic File Assessment (NO LLM)

For each changed source file in `/tmp/DIFF.patch`:
```bash
wc -l <file>                                           # line count
rg -l "from.*<module>.*import" application_sdk/ -t py | wc -l  # callers
rg "def <function_name>" application_sdk/execution/ application_sdk/infrastructure/ -t py -l  # v3 replacement?
rg "warnings\.warn\|DeprecationWarning" <file>          # deprecated?
```

Build annotations: lines, callers, DUMPING_GROUND/V3_REPLACEMENT/DEPRECATED flags.

### 1b. Dispatch Reachability Agent (if review_scope=full or mixed-sdk-toolkit)

Use Agent tool to dispatch `agents/reachability.md` — classifies each
changed symbol as temporal-workflow, temporal-activity, public-http,
internal, test, or dead.

Skip if review_scope is contract-toolkit, conformance-only, tests-only, docs-only, or config-only.
On a delta-scoped re-review (step 11b), run it over the delta files only.

### 1b-toolkit. Private Toolkit Consumer Setup

**Read only when:** `review_scope` is `contract-toolkit` or
`mixed-sdk-toolkit` — then read `sections/toolkit-consumer-setup.md`.
Skip it entirely on every other scope — it is private-repo clone-and-validate setup for surfaces your scope
does not touch, and on a two-file conformance PR it is the single largest
block of context you would carry for no reason.
### 1c. Prepare Context by Tier

**Token budgets per agent call (hard limits — never exceed):**

| Content | Max tokens (approx) |
|---------|-------------------|
| PR diff sent to agent | 60K tokens (~240K chars) |
| Full file contents sent to agent | 30K tokens (~120K chars) |
| Reference rules + preamble | 10K tokens (~40K chars) |
| **Total per agent** | **100K tokens** |

1 token ≈ 4 chars. Measure with `wc -c` and divide by 4.

#### Tier: Full (< 2K lines changed)

Read ALL changed source + test files completely. Send full diff.
This fits within budget for most PRs.

**Safety check:** Before sending to agents, measure total context:
```bash
DIFF_CHARS=$(wc -c < /tmp/DIFF.patch)
FILE_CHARS=0
for f in <changed_files>; do
  FILE_CHARS=$((FILE_CHARS + $(wc -c < "$f")))
done
TOTAL=$((DIFF_CHARS + FILE_CHARS))
echo "Total context: $TOTAL chars (~$((TOTAL/4)) tokens)"
```

If total > 400K chars (~100K tokens): **auto-upgrade to Partitioned tier.**

#### Tier: Partitioned (2K-20K lines changed)

Split files by directory. Each agent gets only its partition:

| Agent | Gets full content of | Gets file list only for |
|-------|---------------------|------------------------|
| CORRECTNESS | `app/`, `execution/`, `credentials/`, `contracts/`, `infrastructure/`, `handler/` | everything else |
| QUALITY | `tests/`, `common/`, remaining source files | high-risk dirs |
| STRUCTURE | top 10 most-changed files (by line count) | everything else |

**Diff splitting:** Each agent gets only the hunks for files in its partition:
```bash
# Extract hunks for specific files from the full diff
grep -A 9999 "^diff --git a/<file>" /tmp/DIFF.patch | \
  sed '/^diff --git a\//q' | head -n -1
```

**Per-agent safety check:** If a partition still exceeds 100K tokens:
- Truncate the LARGEST files: send first 500 lines + last 100 lines + function index
- Format truncated files as:
  ```
  === FILE: path/to/large_file.py (2100 lines, TRUNCATED) ===
  [Lines 1-500]
  <content>

  [Lines 501-2000 OMITTED — function index:]
  - def function_a: line 520
  - class MyClass: line 680
  - def function_b: line 1200

  [Lines 2001-2100]
  <content>
  ```

#### Tier: Staged (20K+ lines changed)

This is for migration PRs, bulk refactors, generated code.

**Step 1: Classify files (deterministic, no LLM):**
```bash
for f in <changed_files>; do
  # HIGH_RISK: critical dirs, public API, security-sensitive
  if echo "$f" | grep -qE "^(application_sdk/(app|execution|credentials|contracts|infrastructure|handler)/|.*__init__\.py$)"; then
    echo "HIGH $f"
  # MEDIUM: other source + tests
  elif echo "$f" | grep -qE "\.(py)$"; then
    echo "MED $f"
  # LOW: docs, config, generated, lock files
  else
    echo "LOW $f"
  fi
done
```

**Step 2: Budget allocation:**
- HIGH_RISK files: full content + their diff hunks (up to 60K tokens)
- MEDIUM files: diff hunks only, no full file content (up to 30K tokens)
- LOW files: skipped entirely

**Step 3: If HIGH_RISK alone exceeds 60K tokens:**
- Sort HIGH_RISK by line count descending
- Include files until budget is reached
- Remaining HIGH_RISK files: downgrade to MEDIUM treatment (hunks only)

**Step 4: Note in review comment:**
```
> **Large PR (N files, M lines).** Full review applied to X high-risk files.
> Hunk review applied to Y medium-risk files. Z low-risk files skipped.
> Re-run `@sdk-review` on specific files if needed.
```

#### Single-file overflow (any tier)

If ANY single file exceeds 2000 lines:
```
=== FILE: path/to/huge_file.py (3500 lines, SUMMARIZED) ===

[Imports: lines 1-45]
<content>

[Class/function index:]
- class App(Base): line 50 (400 lines)
  - def __init__: line 52
  - def run: line 102
  - def _register: line 300
- def helper_a: line 460
- def helper_b: line 520
...

[Changed sections with 50-line context above/below:]
--- Section at lines 142-195 (CHANGED) ---
<full content of lines 92-245>

--- Section at lines 1200-1230 (CHANGED) ---
<full content of lines 1150-1280>

[Unchanged sections omitted]
```

This ensures agents see the structure + the actual changes, without
blowing up the context with 3500 lines of unchanged code.

#### Never fail on large diffs

If despite all truncation the context STILL exceeds limits:
1. Drop STRUCTURE agent (least critical)
2. Drop GPT adversarial
3. Send only the diff (no full file contents) to remaining agents
4. Note in review: "Context truncated due to PR size. Some issues may be missed."

**A truncated review is always better than a failed review.**

Print: `[Phase 1 complete] <N> files assessed, tier=<tier>, <M> files truncated`
then `bash /tmp/budget.sh` — act on its verdict before entering Phase 2.

---

## Phase 2: Review (budget from tier table)

### 2a. Wave 1 — Opus Domain Agents (parallel, native)

Based on `review_scope`, dispatch agents via the Agent tool:

| review_scope | Agents dispatched |
|---|---|
| `full` | correctness.md + quality.md + structure.md (all 3) |
| `minor` | correctness.md only (fast path — keeps guardrail coverage) |
| `contract-toolkit` | toolkit-review.md only |
| `mixed-sdk-toolkit` | correctness.md + quality.md + structure.md + toolkit-review.md |
| `tests-only` | quality.md only |
| `tests-focused` | quality.md + correctness.md (lightweight) |
| `conformance-only` | conformance.md only (conformance-suite specialist) |
| `config-only` | ci-config.md only (CI/workflow/deps/infra specialist) |
| `docs-only` | SKIP Phase 2 entirely |

**Re-review delta scoping (step 11b) modulates this table, not replaces
it**: on a delta-scoped re-review the same routing applies but classified
over `/tmp/DELTA_FILES.txt`, agents receive `/tmp/DELTA.patch` +
`/tmp/PRIOR_REVIEW.md` + full contents of delta files only, and a
verification-only re-review (empty delta) skips Wave 1 AND Wave 2
entirely — Phase 2 reduces to §2e labeling + §2h verdict.

**Mixed partitions:** when a `full` or `tests-focused` PR ALSO changes config
or conformance files, additionally dispatch the matching specialist scoped to
**only** that partition — never hand it to the SDK domain agents:
- `CONFIG_FILES > 0` (`.github/**`, `pyproject.toml`, `uv.lock`, `.pre-commit*`,
  `helm/**`) → also dispatch `ci-config.md` on the config slice. (Skip if the
  only config file is incidental `uv.lock` churn with no `.github/`/`helm/`/
  `pyproject` change.)
- `CONF_FILES > 0` (`packages/conformance/**`, `remediation/**`) → also
  dispatch `conformance.md` on the conformance slice.

The SDK domain agents (correctness/quality/structure) review `application_sdk/**`
+ tests and must NOT be handed `.github/**`, `helm/**`, or
`packages/conformance/**` for Temporal/Dapr review.

Each agent receives: PR diff (or partition), full file contents,
holistic annotations, reachability output. It reads its own reference rules
(named at the top of its `agents/*.md`) — you do not hand them over, and per
§6d you no longer hold them.
For `mixed-sdk-toolkit`, partition context by path: SDK agents receive only
`application_sdk/**`, SDK tests, and config files relevant to SDK behavior;
`toolkit-review.md` receives `contract-toolkit/**` and toolkit-related config.
Do not send `contract-toolkit/**` content to SDK agents for Temporal/Dapr
review.
For `toolkit-review.md`, also pass `contract-toolkit/AGENTS.md`,
`.mothership/pr-review/references/toolkit-consumer-registry.md`,
`/tmp/TOOLKIT_CONSUMERS.md` when present, `/tmp/TOOLKIT_VALIDATION.md`,
`/tmp/TOOLKIT_PR_ARTIFACTS.txt`, and `/tmp/TOOLKIT_ROVER_NOTE.md` when
present. The toolkit agent must not include private consumer repo names, paths,
or SHAs in public findings.

**A dispatch cannot be interrupted, so this is your LAST decision point.**
Once Wave 1 is dispatched you regain control only when the agents return —
`budget.sh` runs at phase boundaries, and the next boundary is after them.
Measured: 43 minutes inside a single dispatch against a 15-minute hard stop,
which then printed `OVER HARD STOP` to nobody who could act on it. Spend the
check here or not at all.

**Degradation priority** — run `bash /tmp/budget.sh` BEFORE dispatching
Wave 1 and drop agents by the measured percentage, not by feel:

| Measured | Action |
|---|---|
| < 70% | Dispatch all agents for the scope |
| ≥ 70% | Drop STRUCTURE (holistic opinions, least urgent) |
| ≥ 85% | Also drop QUALITY (code patterns; pre-commit catches most) |
| ≥ 100% | CORRECTNESS only, then go straight to Phase 3 |

CORRECTNESS is ALWAYS kept — it carries guardrail coverage G1-G5.

Parse JSON findings from each agent response.

### 2b. Wave 2 — cross-model adversarial (via proxy)

**Read only when:** you are on the mothership sandbox lane.
On `LANE: sdk-loop`, skip `sections/adversarial-wave-2.md` entirely. It curls
`$PROXY_BASE/proxy/litellm/...` with `$PROXY_JWT`; both are sandbox variables
that do not exist on a GitHub Actions runner, so on the loop lane the step
cannot succeed and reading it only pays for context. That lane's resolve phase
contests every finding instead, so the challenge still happens.
### 2c. De-Bias (deterministic)

| Opus (Wave 1) | GPT (Wave 2) | Action |
|---|---|---|
| >= 90% confidence | AGREE or not reviewed | Keep |
| >= 80% confidence | AGREE | Keep |
| >= 80% confidence | DISAGREE | **Drop** |
| >= 80% confidence | PARTIAL | Keep, downgrade severity |
| Not flagged | GPT >= 90% | Keep (blind spot) |
| Not flagged | GPT < 90% | Drop |
| **Guardrail violation** | **Any** | **Always keep** |

If GPT was unavailable or skipped: keep all Opus findings >= 80%.

### 2d. Root-Cause Clustering & Class-Completeness Sweep

Findings arrive atomized — one per file/line — but bugs travel in
**classes**. If you report only the instances the agents happened to
land on, the author fixes them one at a time and the same defect comes
back for another review round (and another, and another). A single
revert-scope bug once cost this repo five review rounds because each
round fixed the one instance reported and never the class. Kill the
whole class in one pass.

Operate on the post-de-bias finding set, before locking the verdict:

1. **Cluster by root cause, not by file.** Two findings share a class
   when the *same* underlying fix would resolve both (e.g. "a multi-file
   writer that reverts only `finding.file`", "an auto-detect that resets
   a customized value to its default on a bare re-run", "an
   externally-derived value interpolated into a shell-out"). Give each
   class a one-line name.

2. **Sweep the whole diff for every sibling.** For each class with >= 1
   confirmed finding, grep the *entire* diff — and the immediate module
   the fix will touch — for other occurrences of the same shape that no
   agent flagged individually. Report each as its own finding and note the
   shared class in its human-visible title/body — e.g. a `class: <name>`
   prefix. This is **prose only**: do not add a `class` field to the finding
   payload — the Phase 3a inline-comment schema rejects unknown fields
   (422). A swept-in sibling inherits the class's severity (it is the same
   defect) — do not re-run it through de-bias.
   ```bash
   # e.g. a revert-scope class found in one writer → check every sibling
   rg -n "finding\.file" /tmp/DIFF.patch
   ```

3. **Gate/flag classes: prove the gate isn't hollow.** When the class
   concerns a check, gate, or flag *added in this PR*, verify it has no
   input for which it silently passes: a gate that returns `passed=true`
   unconditionally, a `forces_*`/escalation flag the model must remember
   to set per-call rather than a structural rule field, a validator with
   an early `return True`. An always-pass path is itself a finding — the
   most expensive kind, because it defeats the safety net rather than
   tripping it.

4. **Report the class, once.** In the summary text, group findings by
   class so the author sees "these six are one bug" and fixes the
   invariant, not the instances. This is the single highest-leverage step
   for holding a PR to 2-3 review rounds instead of 20+.

**Scope — reviewer only.** This step targets the sdk-review reviewer, whose
failure mode is *under*-generalization across serial human review rounds. Do
not port it to the deterministic conformance remediation loop
(`detect-fix-recheck`): that loop already fans out into independent,
individually-gated per-finding fixes, and there per-site independence
(fix-vs-suppress decided per site; uncorrelated model errors that recheck
catches one at a time) is a feature, not a limitation. Clustering the *fixes*
there would trade that robustness away for a round-count problem the
self-iterating loop doesn't have.

### 2e. Delta Tracking (if previous review exists)

The previous review should already be loaded into context in Phase 0
step 6b (`PRIOR_REVIEW` / `/tmp/PRIOR_REVIEW.md`) and used as input
to Phase 2 reasoning. This section is the **labeling pass on the
output**: for each finding in the new review, decide whether it's
RESOLVED / STILL PRESENT / NEW relative to the prior review and tag
it accordingly in the summary.

`/tmp/PRIOR_REVIEW.md` was loaded exactly once, in Phase 0 step 6b —
do NOT re-fetch it here. If it is empty at this point, this is a first
review and this section does not apply.

Labeling rules:
- Finding was in previous review, code at that line CHANGED → **RESOLVED**
- Finding was in previous review, code UNCHANGED → **STILL PRESENT**
- Finding is new (not in previous review) → **NEW**

Include the delta status in the review summary (and inline body
where applicable) so the author sees at a glance what was fixed vs
what remains.

### 2e′. Nit convergence — keep the loop terminating AND the verdict reachable

`@sdk-resolve` (the write counterpart, `.mothership/pr-resolve/`) drives a PR by
looping review→fix→push until `### Findings` is **empty** (nits included). That
loop only terminates if the *nit* stream is **bounded, diff-local, and
actionable**. A reviewer that surfaces a fresh batch of pre-existing optional
nits every pass — or lists observations it recommends no action on — makes that
loop non-terminating: it spins round after round until the sandbox dies with no
hand-off. The three rules below keep nits convergent.

Since `READY_TO_MERGE` now requires an empty `### Findings` (§2h), these rules
carry a second load: an unconvergent nit stream no longer just wastes resolver
rounds, it withholds the approval indefinitely. A `Nit` that survives these
three rules is one the resolver can clear; anything else must not be listed as a
finding at all.

**They apply to `Nit`-tier findings ONLY.** Critical / High / Important
findings — and any regression a pushed fix introduces — are ALWAYS raised, on
any line, including code the resolver just pushed. Never diff-scope, defer, or
suppress a real bug for convergence; the whole-file/reachability review of the
higher tiers is unchanged.

1. **Diff-scope nits.** A `Nit` is valid only on a line the PR's diff **adds or
   modifies**. A nit on pre-existing, untouched code — even in a file the PR
   changed, and even when you were handed the whole file for reachability
   context — is out of scope for THIS PR; withdraw it silently (no inline
   comment, no finding). A PR is not the place to polish code it didn't write.

2. **Re-review monotonicity.** On a re-review (`/tmp/PRIOR_REVIEW.md` non-empty),
   a **new** `Nit` may be raised only on a hunk that **changed since the prior
   review's HEAD**. A line that was reviewable last round and drew no nit must
   not draw a new nit this round — you already saw it and passed it. This
   forbids mining a fresh set of optional nits each pass ("polish the earlier
   pass didn't call out"). Still-present prior nits are carried per §2e;
   Critical/Important/regressions remain exempt.

3. **Actionability gate.** A `Nit` is a finding only if it names a concrete fix
   the author can apply. An observation whose only path forward is *"no action
   needed"*, *"accept the tool/manifest quirk"*, *"keep as-is — defensible
   either way"*, or a pure either/or style preference is **not a finding** — do
   NOT list it under `### Findings`. Put it in `### Strengths`/prose if it's
   worth a mention, never as a finding. A finding the resolver cannot act on can
   never be cleared, so listing it would wedge the loop forever.

Net effect: once the author's real fixes land, a re-review of the same
substantive change returns an **empty** `### Findings` with `READY_TO_MERGE`, and
the resolver converges — typically in 2–3 rounds — handing over a clean PR.
`MAX_ROUNDS` stays the backstop for the rare case where a fix legitimately keeps
spawning new work.

### 2f. Guardrails G1-G7

Check consolidated findings. Any G1/G2/G3/G5 → BLOCKED.
(Guardrail IDs per the table in CLAUDE.md — CI is not a guardrail;
see §2h.)

### 2g. Holistic Path Forward (Critical + High only)

For BLOCKING/CRITICAL/HIGH findings, include a `path_forward` in the
inline comment body:
- **Immediate fix** — "Fix this now, it will break in production. Do X."
- **Temporary fix + follow-up** — "Quick fix: X. Right solution: Y (follow-up ticket)."
- **Wrong approach** — "This approach won't work because X. Instead, do Y."
- **Design decision needed** — "Two valid options: A or B. Needs team discussion."

MEDIUM/LOW/INFO findings: one-line suggested_fix only. No path_forward.

### 2h. Determine Verdict

| Verdict | Condition | approval_recommendation |
|---|---|---|
| BLOCKED | G1/G2/G3/G5 violation | REJECT |
| NEEDS_HUMAN | DESIGN_CHANGE scope | REQUEST_CHANGES |
| NEEDS_HUMAN | `review_scope` is `contract-toolkit` or `mixed-sdk-toolkit`, and non-empty `/tmp/TOOLKIT_ROVER_NOTE.md` exists or `/tmp/TOOLKIT_VALIDATION.md` has any mandatory toolkit compatibility check with status `needs rerun` | REQUEST_CHANGES |
| NEEDS_FIXES | Critical, G4/G6, **any Important, any Nit** | REQUEST_CHANGES |
| READY_TO_MERGE | **`### Findings` is empty — 0 Critical, 0 Important AND 0 Nit** | APPROVE |

CI is not a verdict input and is not reported. `sdk-review-downgrade-on-ci-failure.yml`
strips an approval event-driven the moment any non-review check fails — the only
race-free enforcement, since CI legs routinely finish after the review posts.

`READY_TO_MERGE` is strict: **any** finding still listed under
`### Findings` forces `NEEDS_FIXES`, whatever its tier. A single
Important does it; so does a single `Nit`. The verdict and the
write-side resolver now agree on one bar — §2e′ already promises that
"once the author's real fixes land, a re-review of the same substantive
change returns an **empty** `### Findings` with `READY_TO_MERGE`", and
the resolver has always looped until every finding "nits included" is
cleared. Approving over an open nit made the reviewer the looser of the
two and left the resolver still working on a PR that was already
stamped.

The load this puts on `Nit` discipline is the point, and §2e′ is what
carries it: a `Nit` you list must be one the resolver can actually
clear, or the loop wedges. **Before listing any `Nit`, apply the §2e′
convergence rules** (diff-scope, re-review monotonicity, actionability).
An observation that fails them is not a finding — put it in
`### Strengths` or prose, never under `### Findings`. That was already
the rule; it is now load-bearing for the verdict too.

If you believe an Important should be downgraded, downgrade it
explicitly in §2e with a one-line reason — do not silently approve over
the top of it. Downgrading an Important to a `Nit` no longer buys an
approval, so a finding you genuinely accept must be dropped with its
reason, not demoted.

Print: `[Phase 2 complete] <N> findings across <C> classes, verdict=<verdict>`
then `bash /tmp/budget.sh`.

---

## Phase 3: Submit Review (~30s)

### 3a. Build Payload

For each finding, build the object matching the in-sandbox review
payload schema (used for the inline-comment loop in 3f below).

**Strip fields not in the schema** — the handler will 422 on unknown fields.
Only include: title, pattern_id, severity, category, confidence, file, line,
evidence, attack_path, reachable_from, by_design_check, suggested_fix,
escalate_to_linear. Do NOT include scope, domain_tag, guardrail, path_forward
in the findings array — put those in the summary or inline comment body instead.

### 3b. Inline Comments

For BLOCKING/CRITICAL/HIGH findings, create inline comments:
- `file` and `line` must be in DIFF.patch (added lines only)
- Max 15 inline comments
- For `contract-toolkit` / `mixed-sdk-toolkit` scopes ONLY: write each inline
  body to `/tmp/inline-comments/<n>.md` and the matching path/line metadata to
  `/tmp/inline-comments/<n>.json` before Phase 3f — the staged markdown file
  is the only source allowed for the posted `body`, because it is what the
  redaction gate scans. Never post a toolkit inline comment from an in-memory
  body string. Other scopes have no redaction gate and may post directly from
  the built body — no file staging required.
- Format:
  ```
  **[SEVERITY]** [TAG] — description

  **Evidence:** <quoted code>
  **Path Forward:** <immediate fix / temporary fix + follow-up / design decision>
  **Fix:** <exact code suggestion if PATCH scope>
  ```

### 3c. Verdict-Stamp: Owned by the GHA runner (sandbox does nothing)

There is no mothership-side handler, and the sandbox **does not post
`gh pr review`** and **does not apply labels**. Both happen outside
the sandbox:

- **Approval**: `sdk-review-approve-on-verdict.yml` fires on
  `issue_comment: created` from `mothership-ai[bot]` with the
  `<!-- SDK_REVIEW -->` marker (within ~5s of the verdict comment
  landing). It parses the verdict from the structured
  `<!-- VERDICT: X -->` marker in §3e, applies the
  `sdk-review-approved` / `sdk-review-needs-human` /
  `sdk-review-needs-rebase` labels, sets the `sdk-review` commit
  status, and posts the formal `atlan-ci` approval if the verdict is
  `READY_TO_MERGE`. `sdk-review.yml`'s "Approve PR as atlan-ci" step
  runs the same logic after the SSE stream ends as a fallback —
  idempotency guards (label present + no existing approval) prevent
  double-approval. atlan-ci is in CODEOWNERS, so its approval
  satisfies `require_code_owner_review` on `main`;
  `mothership-ai[bot]` is a GitHub App and can't be.
- **Dismiss on human activity**: `sdk-review-dismiss-on-human.yml`
  fires on `issue_comment` / `pull_request_review` from humans and
  dismisses the atlan-ci approval + strips the label. So the bot can
  unblock merges by itself until a human pushes back.
- **Reset on push**: `sdk-review-reset-on-push.yml` fires on
  `pull_request: synchronize` and strips the label + flips the
  `sdk-review` status to pending on the new HEAD. Branch protection
  separately auto-dismisses the approval (`dismiss_stale_reviews_on_push`).
- **CI-failure downgrade**: `sdk-review-downgrade-on-ci-failure.yml`
  fires on `check_suite: completed`; if a non-sdk-review check
  failed on a HEAD that carries `sdk-review-approved`, it strips
  the label, dismisses the approval, and flips status to failure.

**Implication for the sandbox**: don't `gh pr edit --add-label` or
`gh pr review --approve` from inside the orchestration. The verdict
flows out via the structured marker in the summary comment in §3e;
the GHA layer reads that and does the rest.

The structured verdict marker is the contract. Keep
`<!-- VERDICT: X -->` in sync with `### Verdict: ...` in the summary
template. The token must be one of:
`READY_TO_MERGE`, `NEEDS_FIXES`, `BLOCKED`, `NEEDS_HUMAN`, `NEEDS_REBASE`.

### 3d. Resolve Inline Threads (on APPROVE)

If verdict = READY_TO_MERGE, resolve ALL open inline review threads from
previous SDK Review comments. The handler does NOT do this — you must:

```bash
# Get all review threads
gh api graphql -f query='
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes {
            id
            isResolved
            comments(first: 1) {
              nodes { body author { login } }
            }
          }
        }
      }
    }
  }' -F owner=atlanhq -F repo=application-sdk -F pr=$PR

# For each unresolved thread posted by the bot, resolve it
gh api graphql -f query='
  mutation($id: ID!) {
    resolveReviewThread(input: {threadId: $id}) {
      thread { isResolved }
    }
  }' -F id="<thread_id>"
```

Only resolve threads from bot-posted comments (check `author.login`).
Do NOT resolve threads from human reviewers.

### 3e. Summary

Use this template. The leading `<!-- SDK_REVIEW -->` HTML comment is
the marker the orchestration uses to find prior reviews on subsequent
runs; do NOT remove it. The second marker `<!-- VERDICT: X -->` is the
machine-readable verdict the GHA approval workflows parse — keep it
in sync with the human-readable `### Verdict:` line below. The token
after `VERDICT:` MUST be one of: `READY_TO_MERGE`, `NEEDS_FIXES`,
`BLOCKED`, `NEEDS_HUMAN`, `NEEDS_REBASE`. The third marker
`<!-- REVIEWED_HEAD: <sha> -->` records the 40-char HEAD this review
ran against — step 6c reads it on the next round to compute the
re-review delta; omitting it forces the next round back to a full
review. **Substitute `<HEAD_SHA>` with the verbatim 40-character hex
SHA from the prompt header — write the raw hex characters, never the
literal placeholder text `<HEAD_SHA>`.** The §3f submit step adds a
shell-level safety net (`sed`) in case the LLM writes the placeholder,
but the correct value must come from the reviewed HEAD, not a live
re-fetch. The fourth marker `<!-- ANSWERS_TRIGGER: <comment id> -->`
records **which** `@sdk-review` comment this verdict answers — write
`COMMENT_ID` from the prompt header verbatim, raw digits only. On a
`workflow_dispatch` run `COMMENT_ID` is blank; **omit the whole line**
rather than writing an empty or placeholder value. Two reviews can be
outstanding on one PR at once (this sandbox runs up to 2h while the
resolver's per-round wait is 40 min, and a human can re-tag mid-review),
so a verdict's timestamp alone cannot say which request it answers. The
resolver's push guard reads this marker to tell "the round I am waiting
on has answered" from "an earlier round's verdict landed late"; without
it, it falls back to comparing timestamps and can clear a push while
this review is still running — stranding the verdict you are about to
post. For toolkit scopes, the fifth marker
`<!-- TOOLKIT_ARTIFACT_HASH: <sha256> -->` records the PR-generated
artifact hash from Phase 1b-toolkit so the next round can carry
consumer validation forward; omit the line entirely for non-toolkit
scopes.

```
<!-- SDK_REVIEW -->
<!-- VERDICT: READY_TO_MERGE | NEEDS_FIXES | BLOCKED | NEEDS_HUMAN | NEEDS_REBASE -->
<!-- REVIEWED_HEAD: <HEAD_SHA> -->
<!-- ANSWERS_TRIGGER: <COMMENT_ID> -->            <!-- omit on workflow_dispatch -->
<!-- TOOLKIT_ARTIFACT_HASH: <ARTIFACT_HASH> -->   <!-- toolkit scopes only -->
## SDK <Review | Re-review> (mothership): PR #<number> — <title>
<!-- For review_scope=contract-toolkit, write this heading as:
     "Contract Toolkit <Review | Re-review> (mothership)".
     Keep the SDK_REVIEW marker above unchanged. -->

### Verdict: <READY TO MERGE | NEEDS FIXES | BLOCKED | NEEDS HUMAN REVIEW>

> <2-3 sentence summary. Include the holistic assessment:
>  is this fixing symptoms or causes? What's the right path forward?>

---

### Affected Toolkit Surfaces             <!-- ONLY when review_scope=contract-toolkit or mixed-sdk-toolkit -->
- `<surface>` — <why it is affected>

### Cross-Repo Validation                 <!-- ONLY when review_scope=contract-toolkit or mixed-sdk-toolkit -->
- UI rendering compatibility: validated | not applicable | needs rerun
- Manifest substitution compatibility: validated | not applicable | needs rerun
- Workflow execution contract: validated | not applicable | needs rerun
- Generated SDK input contract: validated | not applicable | needs rerun
- Representative app pattern: validated | not applicable | needs rerun

### Delta from previous review            <!-- ONLY when PRIOR_REVIEW non-empty -->
- **Resolved (<N>)**: <one line per finding the author fixed>
- **Still present (<N>)**: <one line per finding that wasn't addressed>
- **New (<N>)**: <one line per finding introduced by the latest changes>
- **Downgraded (<N>)**: <one line per finding the author successfully
  challenged in an inline thread — explain why it was downgraded>

### Findings

> **Format is MANDATORY: file-by-file grouped bullets.** Do NOT
> substitute a Markdown table (`| Severity | Domain | Where | Summary |`).
> Group findings under a bold file-path header, then one bullet per
> finding starting with the severity. Each bullet MUST include the
> domain tag in square brackets, the line reference (`L<n>` or
> `L<n>-<m>`), a description, and an italicised `*Path: …*` clause
> describing the fix. Findings spanning multiple files appear under
> each affected file's header. Sort files alphabetically; within a
> file, sort by severity (Critical → Important → Nit) then by line
> number. PR-metadata-level findings go under
> a `**PR metadata**` pseudo-header.

**`<path/to/file.py>`**
- **Critical** [SEC] L42 — description. *Path: immediate fix — <what to do>*
- **Important** [ARCH] L88 — description. *Path: follow-up ticket — <why>*
- **Nit** [QUAL] L120 — description. *Path: optional cleanup — <why>*

**`<path/to/other_file.py>`**
- **Nit** [STRUCT] L15 — description. *Path: <…>*

### Holistic Recommendations (if any)
- Root cause assessment: is this PR treating symptoms or causes?
- Suggested approach if the current approach is wrong

### Strengths
- <what the PR does well>

### Review Note                           <!-- ONLY when /tmp/TOOLKIT_ROVER_NOTE.md exists -->
<contents of /tmp/TOOLKIT_ROVER_NOTE.md>

---
**Models:** <primary review model>
**Run:** [view workflow logs + cost](<GHA_RUN_URL>)
```

Fill the model names from the models that actually ran this review —
never hardcode them in this template; stale model names in posted
reviews erode trust in everything else the summary claims.

**Title selection — "Review" vs "Re-review":**
- If `/tmp/PRIOR_REVIEW.md` is empty (or this is the first
  `<!-- SDK_REVIEW -->` comment on the PR) → use **"SDK Review
  (mothership)"**.
- If a prior summary exists → use **"SDK Re-review (mothership)"**.
  This tells the human reading the PR-comment timeline that this
  pass loaded the previous review as context and reasoned about
  deltas (per Phase 0 §6b + §2e), not that it ignored history and
  reran the full review from scratch.
- If `review_scope=contract-toolkit`, replace `SDK` in the visible
  heading with `Contract Toolkit`. Keep `<!-- SDK_REVIEW -->` unchanged
  because the approval workflow parses that stable marker.

**Delta section — only on re-reviews:**
- Omit the entire `### Delta from previous review` block on a first
  review.
- On re-reviews, include the block before `### Findings` so the
  human sees what changed without scrolling. Counts can be zero
  (e.g. "Resolved (0)") if a category is empty — that's information
  too. If a finding moved from Critical → Important because the
  author's inline reply provided new context, list it under
  "Downgraded" with a one-line "why" so the reasoning is traceable.

The trailing **Run:** line is required on every summary. Substitute
`<GHA_RUN_URL>` with the value passed in the prompt header. The link
takes readers to the GitHub Actions run that produced this review,
where they can inspect: the streamed event log (started → action →
complete), the final `cost_usd`, the sandbox + session IDs, and any
warnings. This is your audit trail — never omit it.

### 3f. Submit

There is no mothership-side `submit-review` endpoint. Use the
`gh` CLI directly from the sandbox to post the summary as a PR
comment and each finding as an inline review comment.

```bash
# Redaction gate for toolkit reviews. Public review bodies must not include
# private consumer repo names, scratch paths, or fetched SHAs. If this fires,
# rewrite the public body using capability aliases before posting.
#
# The gate exempts the hex-valued control markers (REVIEWED_HEAD,
# TOOLKIT_ARTIFACT_HASH, …) because they are data the approval chain reads
# back, not prose about a consumer: they carry this repo's own head SHA, which
# is public by construction. Before that exemption existed the 40-hex rule
# rewrote `<!-- REVIEWED_HEAD: <sha> -->` to `[private sha]`,
# sdk_review_approve.py read that as "no marker", and it skipped every label
# and the atlan-ci approval while still exiting 0 — a green run on an
# unapproved PR. Keep the markers machine-readable through this step.
if [ "$review_scope" = "contract-toolkit" ] || [ "$review_scope" = "mixed-sdk-toolkit" ]; then
  .mothership/pr-review/scripts/redact-toolkit-public-review.sh /tmp/review-summary.md
  for body in /tmp/inline-comments/*.md; do
    [ -f "$body" ] || continue
    .mothership/pr-review/scripts/redact-toolkit-public-review.sh "$body"
  done
fi

# ORDER MATTERS — the summary comment goes LAST, after the inline
# comments and the commit status.
#
# The summary is the completion signal that everything downstream keys
# off: `sdk-review-approve-on-verdict.yml` fires on it, the workflow's
# soft-success check treats its presence as "the review was delivered",
# and the Phase 0 §6b replay guard reads its footer to decide whether a
# recovered run has already done this work. Post it first and all three
# read a partial submission — inline findings still unposted, status
# unset — as a completed one. Posting it last makes its presence mean
# what every consumer already assumes it means.

# Inline finding comments — post one per finding via
# `gh api repos/$REPO/pulls/$PR_NUMBER/comments` so each can target a
# specific path + line in the diff. The formal verdict review
# (--approve | --comment) is already submitted in §3c — do NOT submit
# a second `gh pr review` here.
#
# For toolkit reviews, every inline body must already exist under
# /tmp/inline-comments/*.md and must have passed the redaction gate above.
# Post inline comments by reading the body from that staged file only.

# Commit status — NOT set here. §3c is the authority: the GHA layer owns the
# verdict stamp, and `sdk-review-approve-on-verdict.yml` sets the sdk-review
# status when it sees the summary's `<!-- VERDICT: X -->` marker. This block
# used to POST it too, contradicting §3c and racing the workflow for the same
# context. Under @sdk-loop it also 403s outright: that phase holds a token
# with no `statuses` scope, by design.
#
# The one place the sandbox DOES set status is the §6b dedupe path, and only
# because that path posts no summary, so nothing downstream would ever fire.

# Stamp the reviewed HEAD SHA into the REVIEWED_HEAD marker — safety
# net in case the LLM wrote the `<HEAD_SHA>` placeholder literally
# rather than substituting the actual value. headRefOid was fetched
# from the authoritative PR metadata in Phase 0 step 3 and has not
# changed (the stale-SHA guard in step 5 would have exited if it had).
# The sed is idempotent: if the LLM already wrote the real SHA the
# pattern finds no match and the file is unchanged.
#
# Note what this net does NOT cover: it repairs the literal `<HEAD_SHA>`
# placeholder only. The redaction gate above already ran, so a marker
# damaged there is past saving here — the pattern no longer matches and
# the file stays broken. That is why the gate exempts the marker rather
# than relying on a repair downstream of it.
HEAD_SHA_STAMPED=$(jq -r '.headRefOid' /tmp/PR.json)
sed -i "s|<!-- REVIEWED_HEAD: <HEAD_SHA> -->|<!-- REVIEWED_HEAD: ${HEAD_SHA_STAMPED} -->|" /tmp/review-summary.md

# Same safety net for ANSWERS_TRIGGER. COMMENT_ID is from the prompt
# header (set it as a shell var, as Phase 0 step 6b does) and is blank
# on workflow_dispatch — so stamp it, then delete the line outright if
# the value came out empty. An empty marker is worse than none: the
# push guard would read it as "this verdict names a round, and it is not
# yours" and hold the resolver's push for the full stale window.
# Both seds are idempotent — a correctly-written line matches neither.
sed -i "s|<!-- ANSWERS_TRIGGER: <COMMENT_ID> -->|<!-- ANSWERS_TRIGGER: ${COMMENT_ID} -->|" /tmp/review-summary.md
sed -i '/<!-- ANSWERS_TRIGGER: *-->/d' /tmp/review-summary.md

# Summary comment (the body built in 3a, including the
# <!-- SDK_REVIEW --> marker and the <!-- REVIEW_DATA --> JSON) — LAST:
gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file /tmp/review-summary.md
```

Retry once on 5xx from the GitHub API. On 422 (malformed inline
comment because the line is not in the diff), drop that one finding
and continue with the rest.

The review reads no CI state at all — see Phase 0 step 9.
`sdk-review-downgrade-on-ci-failure.yml` owns that entirely.

Print: `[Phase 3 complete] Review submitted`

---

## If You Cannot Finish

Always set the commit status + post the summary comment before
exiting (see Phase 3f — status first, summary last, same order as the
happy path). A PR with no review comment and no status update is the
worst outcome.

Submit minimal:
```json
{
  "approval_recommendation": "REQUEST_CHANGES",
  "summary": "SDK Review (mothership) could not complete: <reason>. Re-trigger with @sdk-review.",
  "findings": []
}
```


---

## Appendix A: Sandbox-only run guards

**Read only when:** you are on the mothership sandbox lane.
On `LANE: sdk-loop`, skip `sections/sandbox-guards.md` entirely. The
Runtime table above already records why the loop lane has no use for it: the
Fence job dismisses duplicate triggers before any model runs, each phase is
one invocation on a fresh runner, and the harness re-aims a moved HEAD. Every
guard in that file answers a problem the loop lane does not have.
