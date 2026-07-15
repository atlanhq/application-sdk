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
attempts**.

**Best-effort, NON-blocking — never skip the review over red CI.** This phase
is a quick head start on the obvious failures, not a gate. If CI is still red
after the cap (or the failure is pre-existing / infra), do **NOT** stop — carry
on to Phase 3 and run the review anyway. A review on red CI is still valuable,
and its findings often explain the failure. CI-green is an *exit* condition
(Phase 3c), not an *entry* condition: a genuinely stuck CI ends the run at
`NEEDS_HUMAN` **with the review posted** — never with no review at all.

Print: `[Phase 1 complete] ci=<green|red-carried|noted-preexisting>`

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
TRIGGER_URL=$(gh pr comment "$PR_NUMBER" --body "@sdk-review") || TRIGGER_URL=""
# Validate the comment actually landed — a swallowed gh failure here would make
# 3b poll forever or latch onto a stale prior review.
case "$TRIGGER_URL" in
  *"#issuecomment-"*) : ;;
  *) echo "::error::Failed to post the @sdk-review trigger comment"; exit 1 ;;
esac
TRIGGER_ID="${TRIGGER_URL##*issuecomment-}"
TRIGGER_TIME=$(gh api "repos/atlanhq/application-sdk/issues/comments/$TRIGGER_ID" --jq '.created_at')
```

> **Re-trigger identity:** `sdk-review.yml` only fires for comments from an
> allowlisted author (OWNER/MEMBER/COLLABORATOR **or `atlan-ci`**). Your
> `@sdk-review` comment must post as one of those — `atlan-ci` is the canonical
> machine identity for programmatic re-triggers. If your sandbox token posts as
> some other bot, the reviewer won't fire and 3b will time out.

### 3b. Wait for the reply
Poll every ~30s (up to ~40 min) for a comment created **after** `TRIGGER_TIME`,
authored by a login containing `mothership`, whose body contains
`<!-- SDK_REVIEW -->`. Take the last such comment across all pages. Never treat
CI comments, human comments, or an older review as the reply.

### 3c. Read the verdict + findings
From the reply body, read the `<!-- VERDICT: X -->` marker and every bullet
under `### Findings` (all severities, **including `Nit`**).

**Stopping condition — strict, nits included:** advance to Phase 4 as
*merge-ready* ONLY when ALL THREE hold at once — `### Findings` is absent/empty
(every severity; a single `Nit` still counts as an open finding), verdict is
`READY_TO_MERGE`, and CI is green. This is stricter than the bot's own verdict,
which tolerates nits — **here a lone nit does NOT let you stop; keep looping and
fix it.** The only other ways this loop ends are the five explicit escalations
(round-cap / re-raised-after-dismiss / ci-stuck / ambiguous-fork / cross-repo),
and every one of them still runs Phase 4 and posts the report. **Never end the
run with open findings and no Phase 4 report on the PR.**

### 3c′. Acknowledge on the PR — visible status (every round that has findings)
A review comment landing on the PR is not, by itself, a signal that anyone is
acting on it — from a watcher's side, the review just sits there. Close that gap:
**before** you start fixing, post ONE concise plain-text status comment so the PR
visibly shows the handoff — that resolve has picked up *this* review and will
re-run `@sdk-review` after it pushes. Carry a stable marker:
```bash
gh pr comment "$PR_NUMBER" --body "$(cat <<EOF
<!-- SDK_RESOLVE_STATUS -->
🤖 **SDK Resolve — round ${R}.** Picked up the latest review: ${N} open finding(s)
(${CRIT} blocking, ${NIT} nit). Fixing them now, then I'll push and re-run
\`@sdk-review\` automatically — I keep looping until zero findings (nits included)
+ green CI + \`READY_TO_MERGE\`. Progress: ${GHA_RUN_URL}
EOF
)"
```
Keep it factual and one comment per round (progress is the point). NEVER write
the literal resolve trigger token in this or any comment (guardrail 9) — only
`@sdk-review` and prose.

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

### 3e. Commit, push, re-green CI (best-effort)
`uv run pre-commit run --files <changed>` → relevant tests → commit specific
files → push. The push resets the reviewer labels/status (expected). Then make a
best-effort pass at greening CI on the new HEAD before re-triggering, so you
don't waste a round on a failure you just introduced — but if CI stays red for a
reason you can't fix, still re-trigger the review (same rule as Phase 1: red CI
never blocks the review). Looping back to 3a posts a fresh `@sdk-review` comment
on the PR — that comment is the visible "re-running the review now" signal the
3c′ acknowledgement promised, so the handoff is observable end to end.

### 3f. Repeat → 3a.

If the round cap is hit with findings still open, or CI cannot be greened after
genuine attempts, stop with verdict `NEEDS_HUMAN` — with the latest review and a
clear note of what's blocking — rather than looping.

Print each round: `[Phase 3 round R] <N> findings → <F> fixed, <D> dismissed`
Print at end: `[Phase 3 complete] rounds=<R>, converged=<yes|no>`

---

## Phase 4: Hand to human review + report (do NOT merge)

**Phase 4 is mandatory and runs on EVERY termination** — merge-ready, round-cap,
re-raised-after-dismiss, ci-stuck, ambiguous-fork, or cross-repo. The run must
never end without a final human-readable summary comment on the PR. The
`gh pr comment` in step 2 is the load-bearing signal for humans; post it even if
the review-request in step 1 fails (e.g. can't request from the author) — do
step 2 regardless, and do it before you exit.

1. **Request human review** (from the prompt header: `REVIEWERS` handles +
   `REQUESTER`). This runs whether the outcome is merge-ready OR `NEEDS_HUMAN` —
   either way the PR is now a human's:
   ```bash
   gh pr edit <PR> --add-reviewer <REVIEWERS>   # ignore "can't request from the author"
   ```
2. **Always** post a final PR comment (`<!-- SDK_RESOLVE_SUMMARY -->` marker)
   that **@-mentions `TAG_LIST`** (the reviewers + the requester) so they know
   it's their turn, and includes: rounds taken, findings fixed vs dismissed (with
   dismissal rationales), final CI + verdict, and — if stopped short — exactly
   what remains and why (round-cap / re-raised-after-dismiss / ci-stuck /
   ambiguous-fork / cross-repo). State plainly whether it's merge-ready (green +
   zero findings incl. nits + `READY_TO_MERGE`) or needs their call. This is the
   human-facing counterpart to the machine block in step 4 — post both, never
   just one.
3. **Do NOT `gh pr merge`.** Leave the merge to a human.
4. Emit this block verbatim (the dispatch script parses it):
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
