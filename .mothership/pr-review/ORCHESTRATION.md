# SDK Review — Orchestration Playbook

Follow these phases EXACTLY. Do not skip phases. Do not reorder.
Print `[Phase N complete]` after each phase, followed by `bash /tmp/budget.sh`
(see Time Budgets — the elapsed number is measured, never estimated).

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

6b. **Load prior review into context (re-review continuity)** — if a
    previous `<!-- SDK_REVIEW -->` summary comment exists on this
    PR, read its full body and write it to `/tmp/PRIOR_REVIEW.md`. The
    body becomes **input** to Phase 2 reasoning (not just a labeling
    reference for §2e at the end): it tells the agents what was flagged
    before, what the author said in response, and what should be
    carried forward, downgraded, or re-checked given the current HEAD.

    ```bash
    # S5: use --paginate --slurp and pipe into standalone jq. The naive
    # --paginate --jq idiom runs the jq filter once PER PAGE, so `last`
    # picks the last match on the FIRST page, not the last match across all
    # pages. On PRs with many comments spanning multiple API pages this
    # would silently load an older review. --slurp collapses pages into one
    # outer array; `.[][]` flattens into a single stream before `last`.
    PRIOR_REVIEW=$(gh api "repos/${REPO}/issues/${PR_NUMBER}/comments" \
      --paginate --slurp 2>/dev/null \
      | jq -r '[.[][] | select(.body | contains("<!-- SDK_REVIEW -->"))] | last | .body // ""')

    if [ -n "$PRIOR_REVIEW" ]; then
      printf '%s\n' "$PRIOR_REVIEW" > /tmp/PRIOR_REVIEW.md
      echo "[bootstrap] prior sdk-review summary found — loaded into /tmp/PRIOR_REVIEW.md"
    else
      : > /tmp/PRIOR_REVIEW.md
      echo "[bootstrap] no prior sdk-review summary — fresh review"
    fi
    ```

    **CRITICAL — jq idiom:** two mistakes to avoid:
    - `--jq '.[] | select(...) | .body' | head -1` collapses the raw
      multiline body to its first line (the `<!-- SDK_REVIEW -->`
      HTML marker) and silently drops the entire review content.
    - `--paginate --jq '[.[] | select(...)] | last'` runs the jq filter
      ONCE PER PAGE, so `last` picks the final match on page 1, not
      across all pages. On a PR with enough comments to span pages,
      this returns a stale older review. Always use `--paginate --slurp`
      and pipe into standalone `jq -r '[.[][] | select(...)] | last'`.

    Every subsequent phase that reasons about the PR (Phase 2 agents,
    cross-model debias, verdict determination) should treat
    `/tmp/PRIOR_REVIEW.md` as additional context when non-empty —
    not because it's authoritative, but because it captures both the
    prior bot's reasoning and (often, in author replies on inline
    threads) the human's response, which materially changes what
    counts as a "new" finding vs a known-and-discussed one.

    **Replay and duplicate-trigger guards are sandbox-only** — Appendix A.
    They exist because mothership recovers a dropped stream by re-running
    this prompt from the top in the SAME sandbox, and because a bot can
    re-trigger a review of a HEAD already reviewed. `@sdk-loop` has neither
    shape: opencode is invoked once per job, and its Fence job decides
    duplicate triggers before any model runs.

6c. **Compute the re-review delta (scope-cutter).** Each review summary
    stamps the HEAD it reviewed as `<!-- REVIEWED_HEAD: <sha> -->` (§3e).
    On a re-review, use it to scope Phase 1–2 to what actually changed —
    re-deriving conclusions about unchanged hunks is the single largest
    waste in multi-round reviews. If step 8 updates the branch (BEHIND),
    re-run this computation after it — the HEAD changes.

    ```bash
    PRIOR_HEAD=$(grep -oE '<!-- REVIEWED_HEAD: [0-9a-f]{40} -->' /tmp/PRIOR_REVIEW.md \
      | grep -oE '[0-9a-f]{40}' || true)

    DELTA_KNOWN=0
    : > /tmp/DELTA.patch
    : > /tmp/DELTA_FILES.txt
    if [ -n "$PRIOR_HEAD" ] && git cat-file -e "$PRIOR_HEAD" 2>/dev/null; then
      DELTA_KNOWN=1
      # Restrict to the PR's own files so base-branch merges between rounds
      # don't inflate the delta. A PR file also touched by a base merge shows
      # base churn — acceptable: it errs toward MORE review, never less.
      gh pr view "$PR_NUMBER" --repo "$REPO" --json files --jq '.files[].path' > /tmp/PR_FILES.txt
      git diff "$PRIOR_HEAD".."$HEAD_SHA" -- $(cat /tmp/PR_FILES.txt) > /tmp/DELTA.patch || true
      git diff --name-only "$PRIOR_HEAD".."$HEAD_SHA" -- $(cat /tmp/PR_FILES.txt) > /tmp/DELTA_FILES.txt || true
    fi
    DELTA_LINES=$(grep -cE '^[+-]' /tmp/DELTA.patch 2>/dev/null || echo 0)
    ```

    `DELTA_KNOWN=0` (no prior review, a pre-rollout summary without the
    marker, or an unfetchable `PRIOR_HEAD`) means **unknown**, not "nothing
    changed" — skip delta scoping and run the full review. Only
    `DELTA_KNOWN=1` activates step 11b.

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


8. **Branch freshness + conflict resolution** (before reviewing):
   ```bash
   MERGE_STATUS=$(jq -r '.mergeStateStatus' /tmp/PR.json)
   ```

   If `BEHIND` — **sandbox only.** `update-branch` writes to the PR branch and
   needs `contents: write`; the `@sdk-loop` review phase holds a token without
   it, so this 403s. On that lane, review the branch as it is and note it in
   the summary — a base merge cannot introduce a finding in the PR's own
   hunks, which is what the review is about. Do not retry, and do not report
   the 403 as a defect.

   ```bash
   # Tier 1: GitHub-side update (merges base into the PR branch)
   gh api "repos/$REPO/pulls/$PR_NUMBER/update-branch" \
     -X PUT -f update_method=merge 2>/dev/null
   sleep 10
   # Re-fetch — SHA changed after merge
   git fetch origin "$HEAD_REF" && git reset --hard "origin/$HEAD_REF"
   # Re-fetch authoritative PR metadata and diff after the update
   gh pr view "$PR_NUMBER" --repo "$REPO" --json number,state,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid,title,body,labels > /tmp/PR.json
   gh pr diff "$PR_NUMBER" --repo "$REPO" > /tmp/DIFF.patch
   ```

   If `CONFLICTING`: do NOT attempt a local merge or push — the review is
   read-only (see Runtime). A conflict-resolution merge is the author's
   decision, not the reviewer's. Submit minimal review: "PR has merge conflicts. Please
   rebase or comment `@sdk-review` after resolving conflicts." Set the
   verdict in §3e to `NEEDS_REBASE` (the structured marker is
   `<!-- VERDICT: NEEDS_REBASE -->`); the GHA layer applies the
   `sdk-review-needs-rebase` label from there. EXIT.

9. **Do not read CI.** Removed, not moved: the review cannot act on a check
   either way — it holds no write scope on this lane — and
   `sdk-review-downgrade-on-ci-failure.yml` already enforces CI against the
   verdict event-driven, which is the only race-free way to do it. CI legs
   routinely finish AFTER a review posts, so a reviewer-side snapshot was
   always a stale fact reported next to a verdict it could not influence.
   Under `@sdk-loop` the prep phase owns branch and check state before the
   first review starts. Spend no turn on `gh pr checks`.

10. Read the repo's `CLAUDE.md` for project conventions.

11. **Smart agent routing** — classify the PR by which area it touches, and
    dispatch only the matching specialist(s):
    ```bash
    gh pr view "$PR_NUMBER" --repo "$REPO" --json files --jq '.files[].path' > /tmp/PR_FILES.txt
    TOTAL_FILES=$(wc -l < /tmp/PR_FILES.txt)
    CT_FILES=$(grep -cE '^contract-toolkit/' /tmp/PR_FILES.txt || true)
    SDK_FILES=$(grep -cE '^application_sdk/' /tmp/PR_FILES.txt || true)
    CONF_FILES=$(grep -cE '^(packages/conformance/|remediation/)' /tmp/PR_FILES.txt || true)
    TEST_FILES=$(grep -cE '^(tests/|contract-toolkit/tests/)' /tmp/PR_FILES.txt || true)
    DOC_FILES=$(grep -cE '^(docs/|contract-toolkit/docs/|.*README\.md$)' /tmp/PR_FILES.txt || true)
    CONFIG_FILES=$(grep -cE '^(pyproject\.toml|uv\.lock|\.pre-commit|\.github/|helm/)' /tmp/PR_FILES.txt || true)
    # Agent-prompt / operational-meta files with NO Temporal/Dapr code surface:
    # the mothership review+resolve playbooks, Claude Code config, and
    # AGENTS/CLAUDE instruction files. Excluded from SOURCE_FILES below so a
    # prompt-only PR is never routed into the full 3-agent SDK panel (the SDK
    # correctness/quality/structure agents have no rules for reviewing prompts).
    META_FILES=$(grep -cE '^(\.mothership/|\.claude/)|(^|/)(AGENTS|CLAUDE)\.md$' /tmp/PR_FILES.txt || true)
    # SOURCE_FILES = files in NONE of the non-source buckets (conformance, tests,
    # docs, config, meta). Computed by exclusion (grep -vc) rather than
    # TOTAL - sum(buckets): a file matching two buckets (e.g. docs/CLAUDE.md is
    # both DOC and META) would be subtracted twice by the arithmetic and could
    # zero out a real source count, silently skipping code review. Mirrors the
    # bucket patterns above (contract-toolkit is intentionally NOT excluded — CT
    # PRs are routed by the first two rules before SOURCE_FILES is consulted).
    SOURCE_FILES=$(grep -vcE '^(packages/conformance/|remediation/|tests/|contract-toolkit/tests/|docs/|contract-toolkit/docs/|pyproject\.toml|uv\.lock|\.pre-commit|\.github/|helm/|\.mothership/|\.claude/)|.*README\.md$|(^|/)(AGENTS|CLAUDE)\.md$' /tmp/PR_FILES.txt || true)
    CHANGED_LINES=$(grep -cE '^[+-]' /tmp/DIFF.patch 2>/dev/null || echo 0)
    # Security-sensitive paths NEVER take the fast path (a 3-line auth/secret
    # change is exactly where a subtle blocker hides).
    SECURITY_PATHS=$(grep -cE '(credential|secret|auth|token|_dapr|_temporal)' /tmp/PR_FILES.txt || true)
    ```
   This file-list based classification includes deleted files; do not classify
   only from `+++ b/` diff headers. Apply in order; **first match wins**:
   - If `CT_FILES > 0 && SDK_FILES == 0 && CONF_FILES == 0 && CONFIG_FILES == 0` → `review_scope=contract-toolkit`
     (toolkit-review.md only; mandatory private consumer validation based on affected surface)
   - If `CT_FILES > 0 && (SDK_FILES > 0 || CONFIG_FILES > 0)` → `review_scope=mixed-sdk-toolkit`
     (standard SDK review agents + toolkit-review.md)
   - If `META_FILES > 0 && SOURCE_FILES == 0 && CONFIG_FILES == 0 && CONF_FILES == 0
     && CT_FILES == 0 && TEST_FILES == 0 && DOC_FILES == 0` → `review_scope=docs-only`
     (agent-prompt / operational-meta files only — `.mothership/**`, `.claude/**`,
     `AGENTS.md`, `CLAUDE.md` — carry no Temporal/Dapr code surface, so the SDK
     domain agents have nothing to review. Take the docs-only skip path: submit
     APPROVE, no Phase 2. Because META is excluded from `SOURCE_FILES`, a PR that
     ALSO touches code routes on the real code — `config-only`, `full`, etc. — so
     the meta files never drag prompt/markdown into the SDK correctness agents,
     and code is never skipped just because prompts changed alongside it.)
   - If `SOURCE_FILES == 0 && CONF_FILES > 0 && CT_FILES == 0` → `review_scope=conformance-only`
     (`conformance.md` agent only — the conformance-suite specialist for
     `packages/conformance/**` + `remediation/**`: SARIF detector correctness,
     rule-catalog consistency, rule scope (sdk/app/both), the two CI gates, and
     the paired remediation program in the SAME PR. NOT the SDK CORRECTNESS
     agent — conformance code is AST/rule logic, not Temporal/Dapr runtime.
     If `CONFIG_FILES > 0` too, also dispatch `ci-config.md` on the config slice.)
   - If `SOURCE_FILES == 0 && TEST_FILES > 0` → `review_scope=tests-only`
     (QUALITY agent only — focused on test patterns, coverage, assertions)
   - If `SOURCE_FILES == 0 && DOC_FILES > 0 && TEST_FILES == 0` → `review_scope=docs-only`
     (Skip Phase 2 — submit APPROVE with "Docs/meta-only PR, no code review needed")
   - If `SOURCE_FILES == 0 && CONFIG_FILES > 0` → `review_scope=config-only`
     (`ci-config.md` agent only — the CI/workflow/deps/infra specialist, NOT
     the SDK CORRECTNESS agent. Reviews GHA injection/permissions/pinning,
     shell robustness, dependency/supply-chain, and `helm/**`.)
   - If `SOURCE_FILES <= 2 && TEST_FILES >= SOURCE_FILES * 3` → `review_scope=tests-focused`
     (QUALITY agent + lightweight CORRECTNESS — mostly tests with a few source changes)
   - If `SOURCE_FILES >= 1 && TOTAL_FILES <= 2 && CHANGED_LINES < 50 && CONF_FILES == 0
     && CT_FILES == 0 && CONFIG_FILES == 0 && SECURITY_PATHS == 0` → `review_scope=minor`
     (**fast path** for a tiny source change: CORRECTNESS agent only — it always
     keeps guardrail coverage G1-G5 — skipping the heavier QUALITY/STRUCTURE
     waves and the adversarial Wave 2, on the Small time budget. NEVER matches
     credential/secret/auth or `_dapr`/`_temporal` seam paths; those fall through
     to `full`.)
   - Otherwise → `review_scope=full` (correctness + quality + structure)

11b. **Re-review delta scoping** (only when `DELTA_KNOWN=1` from step 6c).
    Step 11 decides *which* specialists could run; on a re-review the delta
    decides *how much work actually remains*:

    - `DELTA_LINES == 0` (re-trigger on the same HEAD, or only base merges
      since the prior round) → **verification-only re-review**: skip Wave 1
      and Wave 2 discovery entirely. Phase 2 reduces to §2e labeling of the
      prior findings against the current HEAD plus the §2h verdict — no new
      hunks means no new findings can exist.
    - `0 < DELTA_LINES < 2000` → **delta-scoped re-review**: re-run the
      step 11 bucket classification over `/tmp/DELTA_FILES.txt` (not the
      full PR file list) and dispatch only the matching specialists. Agents
      receive `/tmp/DELTA.patch` as the diff, `/tmp/PRIOR_REVIEW.md`, and
      full file contents for delta files only. Prior findings on unchanged
      hunks carry forward per §2e without re-deriving them; prior findings
      whose lines the delta touched are re-verified (that IS the delta work).
    - `DELTA_LINES >= 2000` → full review — a delta that large is a new PR
      in all but name, and hunk-local reasoning stops being sound.

    Guardrail floor: if ANY delta file is source (per the step 11 buckets),
    the CORRECTNESS agent is always dispatched regardless of what the delta
    classification says — the same G1–G5 floor the `minor` fast path keeps.
    Toolkit delta files additionally keep the Phase 1b-toolkit obligations
    (the carry-forward fast path there handles the unchanged-artifact case).

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

### 1b-toolkit. Private Toolkit Consumer Setup (if review_scope=contract-toolkit or mixed-sdk-toolkit)

Read:

- `contract-toolkit/AGENTS.md`
- `.mothership/pr-review/agents/toolkit-review.md`
- `.mothership/pr-review/references/toolkit-consumer-registry.md`

Classify the affected toolkit surfaces from `/tmp/DIFF.patch`. Then clone or
reuse every mandatory consumer target from the registry. This validation setup
is mandatory for affected surfaces; do not approve if a required check cannot
run.

Reset and create the private validation ledger (these files are written only
here, so the reset lives here — not in Phase 0). It is the source of truth
for toolkit compatibility status and verdict gating:

```bash
rm -f /tmp/TOOLKIT_ROVER_NOTE.md
: > /tmp/TOOLKIT_PR_ARTIFACTS.txt
: > /tmp/TOOLKIT_CHANGED_FILES.txt
: > /tmp/TOOLKIT_CONSUMERS.md
: > /tmp/TOOLKIT_VALIDATION.md
printf '## Toolkit Validation Ledger\n\n' >> /tmp/TOOLKIT_VALIDATION.md
```

**Do not re-run what CI already ran.** The `Contract Toolkit /` CI legs on
this HEAD run the same commands the reviewer used to re-run wholesale
(`PKL tests and invariants`, `Verify generated output`, `Generated Python
lint and SDK imports`). Read them instead:

```bash
gh pr checks "$PR_NUMBER" --repo "$REPO" --json name,conclusion \
  --jq '.[] | select(.name | startswith("Contract Toolkit")) | .name + " " + (.conclusion // "pending")' \
  > /tmp/TOOLKIT_CI_LEGS.txt
```

- Every leg `success` → record the legs as the local-check evidence in the
  ledger. Run a local command ONLY as the substrate for probing CI cannot
  express — guard ablations, scratch collision contracts, comparing
  PR-generated artifacts against a consumer's expectations.
- Any leg missing / pending / failed → run that leg's command locally and
  treat a failure caused by PR code or stale generated output as a finding:

```bash
contract-toolkit/scripts/regenerate-all.sh     # ↔ Verify generated output
contract-toolkit/scripts/check-invariants.sh   # ↔ PKL tests and invariants
(cd contract-toolkit && pkl test tests/*.pkl)  # ↔ PKL tests and invariants
uv run --extra workflows python contract-toolkit/scripts/test-sdk-import.py  # ↔ Generated Python lint and SDK imports
git diff --check
```

If a required command cannot run due to Rover environment/tooling failure,
create `/tmp/TOOLKIT_ROVER_NOTE.md` with the sanitized note below.

Record the evidence privately:

```bash
printf -- '- Generated SDK input contract: validated (CI toolkit legs green on this HEAD; PR-bound probing ran locally)\n' >> /tmp/TOOLKIT_VALIDATION.md
```

Capture PR-generated artifacts as the input to downstream checks. Do not use a
consumer repository's released toolkit dependency as proof for this PR:

```bash
find contract-toolkit/examples -path '*/generated/*' -type f | sort > /tmp/TOOLKIT_PR_ARTIFACTS.txt
git diff --name-only -- contract-toolkit/examples contract-toolkit/src > /tmp/TOOLKIT_CHANGED_FILES.txt
```

**Carry-forward fast path (re-reviews).** Hash the PR-generated artifacts
and compare against the hash the prior review stamped (§3e stamps
`<!-- TOOLKIT_ARTIFACT_HASH: ... -->` on toolkit-scope summaries):

```bash
ARTIFACT_HASH=$(cat /tmp/TOOLKIT_PR_ARTIFACTS.txt | xargs shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
PRIOR_HASH=$(grep -oE '<!-- TOOLKIT_ARTIFACT_HASH: [0-9a-f]{64} -->' /tmp/PRIOR_REVIEW.md \
  | grep -oE '[0-9a-f]{64}' || true)
```

If `PRIOR_HASH` is non-empty and equals `ARTIFACT_HASH`, the generated
artifacts are byte-identical to a commit whose downstream compatibility was
already validated: mark every artifact-derived capability
`validated (carried forward — artifacts byte-identical to previously
validated commit)` in the ledger and **skip the consumer clone loop
entirely**. Carry-forward covers the *consumer-side* checks only — new
toolkit-source behavior the committed examples don't exercise (a new
invariant, a changed codegen skip-list) still needs PR-bound local probing
(scratch contracts, ablations), which requires no clones. Hash mismatch or
no prior hash → full validation below.

Use `/tmp/toolkit-review-consumers` for scratch clones:

```bash
mkdir -p /tmp/toolkit-review-consumers

# Core consumers. Use existing /workspace checkout if present; otherwise clone.
# Record branch and SHA privately in /tmp/TOOLKIT_CONSUMERS.md.
for spec in \
  "atlan-frontend beta" \
  "blaze main" \
  "heracles beta" \
  "atlan-automation-engine-app main"
do
  repo="${spec% *}"
  branch="${spec#* }"
  if [ -d "/workspace/${repo}/.git" ]; then
    target="/workspace/${repo}"
  else
    target="/tmp/toolkit-review-consumers/${repo}"
    if [ ! -d "${target}/.git" ] && ! git clone "https://github.com/atlanhq/${repo}.git" "${target}"; then
      printf '%s\n' "Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge." > /tmp/TOOLKIT_ROVER_NOTE.md
      continue
    fi
  fi
  if ! git -C "${target}" fetch origin "${branch}"; then
    printf '%s\n' "Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge." > /tmp/TOOLKIT_ROVER_NOTE.md
    continue
  fi
  printf '%s %s %s\n' "${repo}" "origin/${branch}" "$(git -C "${target}" rev-parse "origin/${branch}")" >> /tmp/TOOLKIT_CONSUMERS.md
done
```

Cloning/fetching only establishes the validation target. It is not validation.
For each affected capability, run the corresponding minimum actionable check in
the registry using PR-generated artifacts or a scratch contract rewritten to
amend/import `/workspace/application-sdk/contract-toolkit/src/*.pkl`. If no
PR-bound command or inspection is possible, mark that capability `needs rerun`
and do not approve.

Each mandatory capability must append exactly one private status line to
`/tmp/TOOLKIT_VALIDATION.md`:

```text
- UI rendering compatibility: validated (<private evidence recorded>)
- Manifest substitution compatibility: validated (<private evidence recorded>)
- Workflow execution contract: validated (<private evidence recorded>)
- Generated SDK input contract: validated (<private evidence recorded>)
- Representative app pattern: not applicable (<why>)
```

Allowed statuses are `validated`, `not applicable`, and `needs rerun`. Any
`needs rerun` status forces `NEEDS_HUMAN`.
The public review must mirror these as a `### Cross-Repo Validation` section
using only capability aliases and status values. Do not include private
consumer repository names, package names, branch names, SHAs, local paths, or
system-app implementation details in the public section.

For representative app patterns, inspect PR title, body, and diff for trigger
terms from the registry. Clone/fetch only the matching pattern repos. Optional
field additions do not require adoption in the representative app unless the PR
claims compatibility or changes required generated/runtime behavior.

Use these pattern specs after a trigger match:

```bash
# Pattern specs: "<pattern> <repo> <branch>"
# query-intelligence atlan-query-intelligence-app main
# publish atlan-publish-app main
# popularity atlan-popularity-app main
# lineage atlan-lineage-app main
```

For each matched pattern, reuse `/workspace/<repo>` if present, otherwise clone
to `/tmp/toolkit-review-consumers/<repo>`, fetch the listed branch, and append
the private SHA to `/tmp/TOOLKIT_CONSUMERS.md`.

When validating a representative app contract, work only in a scratch copy. If
the contract imports `@app-contract-toolkit/...`, rewrite that scratch copy to
import the PR checkout source under
`/workspace/application-sdk/contract-toolkit/src/` before running `pkl eval`.

Scratch rewrite pattern:

```bash
scratch="/tmp/toolkit-review-consumers/scratch/<pattern>"
mkdir -p "$scratch"
cp -R "<consumer-contract-dir>"/. "$scratch"/
rg -l '@app-contract-toolkit/' "$scratch" \
  | xargs perl -0pi -e 's#@app-contract-toolkit/#/workspace/application-sdk/contract-toolkit/src/#g'
pkl eval -m "$scratch/generated" "$scratch/app.pkl"
```

If the representative app does not have a contract yet, do not fail adoption.
Validate the generic PR-generated artifact shape and record the representative
pattern as `not applicable` with the reason.

All consumer repo names, local paths, and SHAs are private evidence. Public PR
comments may only use capability aliases:

- `UI rendering compatibility`
- `Manifest substitution compatibility`
- `Workflow execution contract`
- `Generated SDK input contract`
- `Representative app pattern`

If clone/fetch/auth/network fails for a mandatory target, create
`/tmp/TOOLKIT_ROVER_NOTE.md` with exactly this public note and continue to
Phase 2 so the review can request a rerun or human review:

```text
Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge.
```

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

### 2b. Wave 2 — GPT-5.3-codex Adversarial (via proxy)

After Wave 1, call GPT to challenge your findings.

**Skip conditions** (no adversarial):
- `review_scope` is tests-only, conformance-only, config-only, docs-only, or minor
- `review_scope` is contract-toolkit and toolkit-review.md produced zero findings
- `review_tier` is "staged" (massive PR — too much context for one GPT call)
- Wave 1 produced zero findings (nothing to challenge)
- Time budget already over 70% consumed — run `bash /tmp/budget.sh` here
  and skip whenever it prints `OVER 70%` or `OVER HARD STOP`. This is the
  single most expensive optional step in the run (a full GPT-5.3-codex
  call over the whole diff plus every Wave 1 finding), so it is the first
  thing an over-budget run must give up.

If not skipped:

```bash
curl -s "$PROXY_BASE/proxy/litellm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "temperature": 0.2,
    "max_tokens": 16000,
    "messages": [
      {"role": "system", "content": "<agents/adversarial.md content>"},
      {"role": "user", "content": "<Wave 1 findings + PR diff + annotations>"}
    ]
  }'
```

GPT challenges every Opus finding. GPT also discovers findings Opus missed.

If GPT unavailable or skipped: keep all Opus findings >= 80%.
Note in review: "Cross-model adversarial: <skipped (reason) | ran | unavailable>."

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

Everything here is for the **mothership sandbox**. `@sdk-loop` skips this
section entirely — its harness does the same jobs in Python, before any model
runs, which is both cheaper and not subject to an agent deciding to skip a
step. Kept out of the main flow so the common path stays readable rather than
carrying ~120 lines that two thirds of runs must reason past.

A1. **Reset per-run review artifacts** — these files are load-bearing signals
    across later phases, so never let a prior iteration in the same sandbox
    affect the current verdict or public post:
    ```bash
    mkdir -p /tmp/inline-comments
    find /tmp/inline-comments -type f \( -name '*.md' -o -name '*.json' \) -delete
    ```
    The toolkit ledger files (`/tmp/TOOLKIT_*.md|txt`) are reset at the top of
    Phase 1b-toolkit — the only place that writes them — so non-toolkit scopes
    don't touch them at all.

A2. **Stale SHA guard** — bail if the PR has moved since dispatch:
   ```bash
   CURRENT_SHA=$(jq -r '.headRefOid' /tmp/PR.json)
   if [ "$CURRENT_SHA" != "$HEAD_SHA" ]; then
     echo "PR moved from $HEAD_SHA to $CURRENT_SHA since dispatch — aborting cleanly."
     # Submit a minimal review so the status check doesn't stay pending,
     # then exit. A fresh @sdk-review on the new HEAD gets a new session.
     exit 0
   fi
   ```


**A3. Same-run replay guard — run this immediately after loading
`/tmp/PRIOR_REVIEW.md`, before anything else.** When the Claude
stream drops mid-run (sandbox VPN reconnect, container eviction,
transient API error), mothership recovers by re-running this prompt
**from the top in the same sandbox** — a fresh run, not a `--resume`.
If the first pass had already posted its summary, that retry loads
its own summary as `PRIOR_REVIEW`, sees an empty delta, and posts a
SECOND summary for the same HEAD. The PR then shows two reviews with
different wording from a single `@sdk-review` trigger, and the
workflow's soft-success check passes because *a* summary exists.

Every summary's footer carries the run URL that produced it (§3e),
and §3f posts the summary **last**, after the inline comments and the
commit status — so a summary bearing this run's URL means this run's
submission completed in full. Check *every* summary on the PR, not
just the one 6b loaded: that one is only the latest, and an unrelated
review landing between the first pass and the replay would hide this
run's footer behind it.

```bash
# GHA_RUN_URL is given in the session prompt — substitute it literally,
# shell variables do not persist between Bash calls.
gh api "repos/${REPO}/issues/${PR_NUMBER}/comments" --paginate --slurp 2>/dev/null \
  | jq -r '[.[][] | select(.body | contains("<!-- SDK_REVIEW -->")) | .body] | join("\n")' \
  | grep -qF '<GHA_RUN_URL>' \
  && echo "REPLAY: this run already posted its review summary"
```

If that prints `REPLAY`, **stop the entire run here**: post nothing,
set no commit status, resolve no threads, do not continue to Phase 1.
The review was already delivered by the pass that died.

Key the guard on the run URL, never on `HEAD_SHA` alone — a genuine
re-trigger against an unchanged HEAD comes from a *different* run and
must still produce a review.

This guard only catches a replay that re-enters the prompt from the top.
A provider-level retry that replays a single assistant *turn* re-executes
the `gh api … /comments -f body=…` call directly, with no prompt to
re-read — that is how #3276 collected the identical summary five times.
Nothing you can write here prevents it, so the workflow cleans up after
it: `sdk_review_dedupe_verdicts.py` runs once the stream closes, keeps the
newest summary this run posted and minimizes the rest (FND-636). It
identifies "this run's" summaries by the `<GHA_RUN_URL>` in the §3e
footer, the same key this guard greps — which is another reason that line
is not optional. Drop it and the collapse silently stops working.

**A4. Bot-trigger dedupe guard — run immediately after the replay guard.**
This is now a *backstop*, not the authority. Since FND-636 the
authoritative check is the `Dedupe check` step at the top of
`sdk-review-dispatch`, which runs under the per-PR concurrency lock and
declines before a sandbox is booted at all — deciding it here meant the
sandbox had to start and reason before it could decline, so the run was
paid for either way, and a degraded model skipped the check entirely
(five duplicate triggers on #3285 became five runs). Keep this guard: it
costs one API call, and it still catches the case where the comment
landed between the workflow's read and the sandbox's start.

Check: if `COMMENTER` is an automated trigger (`mothership-ai[bot]`
or `atlan-ci`) **and** the newest summary's `<!-- REVIEWED_HEAD -->`
equals `HEAD_SHA`, this run is a duplicate. Stop without posting.
Humans (`COMMENTER` is any other login) are never stopped by this
guard — re-reading the same diff is a legitimate human request.

Use "newest summary" (the body already in `/tmp/PRIOR_REVIEW.md`),
never the oldest.

```bash
# COMMENTER and HEAD_SHA are from the prompt header.
BOT_TRIGGERS="mothership-ai[bot] atlan-ci"
if echo "$BOT_TRIGGERS" | grep -qF "$COMMENTER"; then
  NEWEST_REVIEWED_HEAD=$(grep -oE '<!-- REVIEWED_HEAD: [0-9a-f]{40} -->' /tmp/PRIOR_REVIEW.md \
    | grep -oE '[0-9a-f]{40}' || true)
  if [ -n "$NEWEST_REVIEWED_HEAD" ] && [ "$NEWEST_REVIEWED_HEAD" = "$HEAD_SHA" ]; then
    echo "SKIP: bot-trigger dedupe — @${COMMENTER} re-triggered on HEAD ${HEAD_SHA} which the newest summary already reviewed. Backstop for the workflow's locked dedupe check. Stopping without posting."
    # S4: Restore the commit status so the dispatch run's pending state
    # does not linger after this no-op. The dispatch run always sets
    # sdk-review to "pending" before the sandbox starts. If the sandbox
    # exits here without posting a new verdict comment, the fast-path
    # approve workflow never fires, and the pending status persists —
    # which is misleading (the review was already delivered). Re-apply
    # the status implied by the prior verdict.
    PRIOR_VERDICT=$(grep -oE '<!-- VERDICT: [A-Z_]+ -->' /tmp/PRIOR_REVIEW.md \
      | grep -oE '[A-Z_]+' | head -1 || true)
    case "$PRIOR_VERDICT" in
      "READY_TO_MERGE")
        gh api "repos/${REPO}/statuses/${HEAD_SHA}" -X POST \
          -f state=success -f context=sdk-review \
          -f description="Approved (bot-retrigger skipped: already reviewed this HEAD)" 2>/dev/null || true;;
      "")
        : # No prior verdict found — leave status as-is to avoid stomping
        ;;
      *)
        gh api "repos/${REPO}/statuses/${HEAD_SHA}" -X POST \
          -f state=failure -f context=sdk-review \
          -f description="Verdict: ${PRIOR_VERDICT} (bot-retrigger skipped: already reviewed this HEAD)" 2>/dev/null || true;;
    esac
    exit 0
  fi
fi
```

