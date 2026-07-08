# SDK Resolver — Orchestration Playbook

Drive the PR to **merge-ready**, then stop. This is a server-side port of the
`automate-pr` skill's CI-green + `@sdk-review` loop, run in a mothership sandbox
instead of on a laptop. The reviewer runs in its OWN separate sandbox — you only
trigger it and consume its output.

Read `PR_NUMBER`, `GHA_RUN_URL`, `MAX_ROUNDS` (default 8) from the prompt header.
Print `[Phase N complete] …` after each phase. Hard stop: the sandbox caps at
2h — if you approach it, jump to Phase 4 and report.

Never fabricate a CI result or a reviewer comment. Always read real state from
`gh` before deciding the next action.

---

## Phase 0: Preflight

```bash
cd /workspace/application-sdk
echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
gh pr checkout "$PR_NUMBER"          # get onto the PR head branch (push target)
gh pr view "$PR_NUMBER" --json number,headRefName,baseRefName,url,headRefOid,isCrossRepository
uv sync --all-extras 2>/dev/null &   # warm deps for pre-commit/pytest
```
If `isCrossRepository == true` (a fork you can't push to) → post a comment
explaining the resolver only supports same-repo PRs, and stop.

Print: `[Phase 0 complete] pr=<N> head=<branch>`

---

## Phase 1: CI green

```bash
gh pr checks "$PR_NUMBER" --watch
```
For each failing **required** check: find the run, `gh run view <id> --log-failed`,
then:
- **In-PR, obvious fix** (pre-commit/lint, type error, missing import, a unit
  test this PR broke) → fix it.
- **Pre-existing / infra flake** (already red on main, OOM, timeout unrelated to
  the diff) → do NOT touch; note it and move on.

If you changed files: `uv run pre-commit run --files <changed>`, run the relevant
tests, then commit + push (see Commit rules). Re-watch and repeat. Cap: **5 fix
attempts**; if still red for an in-PR reason, stop and report.

Print: `[Phase 1 complete] ci=<green|noted-preexisting>`

---

## Phase 2: Resolve Copilot review threads (if any)

Fetch unresolved, non-outdated review threads whose first comment author login
contains `copilot` (GraphQL — see the `automate-pr` skill for the exact query).
For each: read the code, **fix** or **dismiss as false-positive with a
rationale**, always reply on the thread, then resolve the thread via
`resolveReviewThread`. Push any fixes. Cap: **3 rounds**, then move on.

Print: `[Phase 2 complete] copilot=<N fixed>/<M dismissed>`

---

## Phase 3: The `@sdk-review` loop (core)

Repeat up to `MAX_ROUNDS`:

### 3a. Trigger the reviewer (its own sandbox)
```bash
TRIGGER_URL=$(gh pr comment "$PR_NUMBER" --body "@sdk-review")
TRIGGER_ID="${TRIGGER_URL##*issuecomment-}"
TRIGGER_TIME=$(gh api "repos/atlanhq/application-sdk/issues/comments/$TRIGGER_ID" --jq '.created_at')
```

### 3b. Wait for the reply
Poll every ~30s (up to ~40 min) for a comment created **after** `TRIGGER_TIME`,
authored by a login containing `mothership`, whose body contains
`<!-- SDK_REVIEW -->`. Take the last such comment across all pages. Never treat
CI comments, human comments, or an older review as the reply.

### 3c. Read the verdict + findings
From the reply body, read the `<!-- VERDICT: X -->` marker and every bullet
under `### Findings` (all severities, **including `Nit`**).

**Stopping condition:** `### Findings` absent / empty AND verdict
`READY_TO_MERGE` AND CI green → done, go to Phase 4. (This is stricter than the
bot's own verdict, which tolerates nits — here nits count.)

### 3d. Fix every finding (or prove it false)
For each bullet, incl. every nit:
- Locate the file/line, read enough context, apply the **minimal** fix the
  `Path:` clause describes.
- If genuinely wrong: reply with a concrete rationale (why it's a false
  positive) instead of editing. **If this exact finding was already dismissed
  in a prior round and the reviewer re-raised it → stop, verdict `NEEDS_HUMAN`,
  report it. Do not loop.**
- Genuinely ambiguous design fork with no clear winner → leave it, note it for
  human, keep going with the rest.

### 3e. Commit, push, re-green CI
`uv run pre-commit run --files <changed>` → relevant tests → commit specific
files → push. The push resets the reviewer labels/status (expected). Re-run
Phase 1's CI-green loop on the new HEAD **before** re-triggering — don't waste a
review round on a known CI failure.

### 3f. Repeat → 3a.

Print each round: `[Phase 3 round R] <N> findings → <F> fixed, <D> dismissed`
Print at end: `[Phase 3 complete] rounds=<R>, converged=<yes|no>`

---

## Phase 4: Stop at merge-ready + report (do NOT merge)

1. Post a final PR comment: rounds taken, findings fixed vs dismissed (with the
   dismissal rationales), final CI + verdict, and — if stopped short — exactly
   what remains and why (round cap / re-raised-after-dismiss / ambiguous fork).
2. **Do NOT `gh pr merge`.** Leave the merge to a human.
3. Emit this block verbatim (the dispatch script parses it):
   ```
   === SDK RESOLVE SUMMARY ===
   pr: <N>
   rounds: <R>
   findings_fixed: <N>
   findings_dismissed: <N>
   ci: <green|red|noted-preexisting>
   final_verdict: <READY_TO_MERGE|NEEDS_HUMAN|NEEDS_FIXES|...>
   merge_ready: <yes|no>
   stopped_reason: <converged|round-cap|re-raised-after-dismiss|ci-stuck|fork>
   === END SUMMARY ===
   ```

Print: `[Phase 4 complete] merge_ready=<yes|no>`

---

## Principles

- **Merge-ready, not merged.** Green CI + zero findings + `READY_TO_MERGE`, then
  hand back to a human.
- **The reviewer stays read-only.** You are the only writer; it runs in its own
  sandbox and you consume its comment output.
- **Converge or escalate.** Never loop forever — round cap, re-raise-after-
  dismiss, and ambiguous forks all stop cleanly with a `NEEDS_HUMAN` report.
- **Real state only.** Read `gh` before every decision; never simulate a CI or
  reviewer result.
