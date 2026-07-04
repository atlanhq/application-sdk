---
name: automate-pr
description: >-
  Drive an already-open PR to merge-ready: fix CI failures, resolve GitHub
  Copilot review comments, then loop @sdk-review (mothership) — fixing every
  finding it reports, including nits — until it comes back clean.
---

# /automate-pr

**Prerequisite:** a PR is already open for the branch you're working on. This
skill does not create PRs — it drives an existing one to a mergeable state.

## Usage

```
/automate-pr          # operates on the current branch's open PR
/automate-pr 1234     # operate on PR #1234
/automate-pr <url>    # operate on PR by URL
```

## Overview

Four phases, run in order:

1. **CI green** — fix any failing checks.
2. **Copilot clean** — resolve every unresolved GitHub Copilot review comment.
3. **Mothership clean** — post `@sdk-review`, wait for the real bot reply, fix
   every finding it lists (including nits), push, and repeat until it reports
   zero findings.
4. **Report** — summarize what changed.

This is a real GitHub-in-the-loop workflow. Never fabricate a CI result or a
mothership comment — always read the actual state from `gh`/the GitHub API
before deciding the next action.

## Phase 0: Identify the PR

```bash
# If an argument was given (number or URL), pass it to `gh pr view <arg>`.
gh pr view --json number,headRefName,baseRefName,url,headRefOid,isDraft
gh repo view --json nameWithOwner --jq .nameWithOwner
```

Store `PR_NUMBER`, `HEAD_BRANCH`, `BASE_BRANCH`, `REPO` (owner/name). Confirm
`gh auth status` is logged in before doing anything else.

If the PR is a draft, note it but proceed — the point of this skill is to get
it into mergeable shape.

---

## Phase 1: CI green

```bash
gh pr checks "$PR_NUMBER" --repo "$REPO" --watch
```

For each failing required check:

1. Find the run: `gh pr checks "$PR_NUMBER" --repo "$REPO" --json name,state,link,workflow`
2. Read the failure: `gh run view <RUN_ID> --repo "$REPO" --log-failed`
3. Decide:
   - **In PR code, obvious fix** (pre-commit/lint, type error, missing import,
     a unit test broken by this PR's change) → fix it.
   - **Pre-existing / infra failure** (flaky test unrelated to the diff, OOM,
     timeout, an already-red check on `main`) → do not touch it. Note it and
     move on — do not let it block the loop, but call it out in the final
     report.
4. If you changed files: run `uv run pre-commit run --files <changed files>`,
   then relevant tests (`uv run pytest ...`), then commit and push (see
   **Commit conventions** below).
5. Re-run `gh pr checks --watch` and repeat.

Cap this at **5 fix attempts**. If CI is still red on required checks after 5
attempts and the failures aren't clearly pre-existing, stop and ask the user
how to proceed rather than guessing further.

Do not proceed to Phase 2 while a required check is failing for a reason
inside this PR's diff.

---

## Phase 2: Resolve GitHub Copilot review comments

Copilot's automated PR review posts as a bot account whose login contains
`copilot` (e.g. `copilot-pull-request-reviewer[bot]`). Fetch unresolved
threads from that author via GraphQL:

```bash
gh api graphql -f query='
query($owner:String!, $repo:String!, $pr:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$pr) {
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first:10) {
            nodes {
              id
              databaseId
              author { login }
              body
              path
              line
              originalLine
              diffHunk
              url
            }
          }
        }
      }
    }
  }
}' -f owner="${REPO%/*}" -f repo="${REPO#*/}" -F pr="$PR_NUMBER"
```

- Keep threads where `isResolved == false && isOutdated == false` and the
  first comment's `author.login` matches `copilot` (case-insensitive).
- If none remain, skip to Phase 3.

For each unresolved Copilot thread:

1. Read the comment body, the `diffHunk`, and the actual file at `path`
   around `line`/`originalLine`. Pull in more surrounding context/callers if
   needed to judge it properly.
2. **Decide fix vs. false positive:**
   - **Fix**: the concern is valid — edit the code. Minimal, targeted change;
     don't refactor around it.
   - **False positive**: not applicable, already handled, or intentional —
     no code change.
3. **Always reply on the thread** (this is required either way):
   ```bash
   gh api repos/$REPO/pulls/$PR_NUMBER/comments/$COMMENT_DATABASE_ID/replies \
     -f body="<reply>"
   ```
   - If fixed: a short factual note, e.g. "Fixed — now checks for `None`
     before dereferencing." (no need to over-explain).
   - If dismissed: a concise rationale, e.g. "False positive: `x` is
     validated non-null by the caller in `foo.py:42`."

After working through all threads, if any code changed: `uv run pre-commit
run --files <changed>`, commit, push (see **Commit conventions**).

Copilot may post a fresh review after your push. Re-run the fetch once more
after pushing; if new unresolved Copilot threads appear, repeat. Cap at **3
rounds** — if still unresolved after that, note it in the final report and
move on to Phase 3 rather than looping forever.

---

## Phase 3: Mothership (`@sdk-review`) loop

This is the core loop. `@sdk-review` dispatches a real bot (`mothership-ai`)
that reviews the PR out-of-band and posts a comment back — this can take
several minutes. **Wait for the actual comment; never simulate or guess its
contents.**

### 3a. Trigger

```bash
TRIGGER_URL=$(gh pr comment "$PR_NUMBER" --repo "$REPO" --body "@sdk-review")
TRIGGER_ID="${TRIGGER_URL##*issuecomment-}"
TRIGGER_TIME=$(gh api "repos/$REPO/issues/comments/$TRIGGER_ID" --jq '.created_at')
```

### 3b. Wait for the reply

Poll for a comment created after `TRIGGER_TIME`, authored by a login
containing `mothership`, whose body contains the `<!-- SDK_REVIEW -->`
marker:

```bash
gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate \
  --jq --arg after "$TRIGGER_TIME" \
  '[.[] | select(.created_at > $after)
        | select(.user.login | test("mothership"; "i"))
        | select(.body | contains("<!-- SDK_REVIEW -->"))] | last'
```

Poll every ~30s. A review can take up to ~35 minutes on a large PR, so budget
accordingly: run this check in a loop inside one Bash call for a few minutes,
and if nothing shows up, issue the same check again — repeat up to a **total
wait of ~40 minutes**. If nothing arrives after that, stop and tell the user
the mothership workflow may not have fired (check the repo's Actions tab)
rather than waiting indefinitely.

**Never treat CI comments, human comments, or old `sdk-review` comments as
the reply.** It must be newly created after `TRIGGER_TIME`.

### 3c. Read the verdict

Read the full comment body (use `--jq '.last.body'` or fetch by id and print
it — don't truncate with naive `head -1`/single-line jq selectors, the body
is multiline). Extract:

- The `<!-- VERDICT: X -->` marker.
- The `### Findings` section — every bullet under it, grouped by file, at
  every severity (`Critical`, `Important`, `Nit`).

**Stopping condition:** if `### Findings` is absent, says there are no
findings, or has zero bullets, the PR is clean — stop the loop and go to
Phase 4. This bar is stricter than the bot's own `VERDICT` (which tolerates
nits) — per this skill's requirement, **nits count too**.

### 3d. Fix every finding

For each finding bullet (yes, including every `Nit`):

1. Locate the file and line it points to.
2. Read enough surrounding context to understand it correctly.
3. Apply the fix the bullet's `*Path: ...*` clause describes. Make the
   minimal change that addresses it — don't use it as an excuse to refactor
   nearby code.
4. If a finding is genuinely ambiguous — e.g. it explicitly presents two
   valid design options with no clear winner — use `AskUserQuestion` once to
   get a decision rather than guessing. Don't stall the whole loop on this;
   ask, get the answer, keep going.
5. If a finding turns out to already be resolved (code no longer matches
   what's described, e.g. from an earlier Copilot fix) or is a duplicate,
   skip it and note why in the round summary — don't silently drop findings
   without a reason.

### 3e. Commit, push, re-verify CI

```bash
uv run pre-commit run --files <changed files>
uv run pytest <relevant test paths> -x
git add <specific changed files>   # never `git add -A`
git commit -m "fix(review): address sdk-review findings (round N)"
git push
```

Then re-run **Phase 1's CI-green loop** on the new push before re-triggering
— don't burn a mothership review cycle on a CI failure you already know
about.

### 3f. Repeat

Go back to **3a** (post `@sdk-review` again) and continue until 3c's stopping
condition is met.

Cap this at **8 rounds** total. If findings still remain after 8 rounds, stop
and report exactly what's left, and ask the user how to proceed — don't loop
forever on something that isn't converging.

---

## Commit conventions

- Conventional Commits format (`fix:`, `chore:`, `fix(review):`, etc.) per
  `docs/standards/commits.md`.
- **Never add a `Co-Authored-By` line** — this repo's `CLAUDE.md` explicitly
  overrides the default.
- **Never edit `CHANGELOG.md`** — it's CI-generated.
- Stage specific files (`git add <paths>`), never `git add -A`/`.`.
- Never force-push.
- Always run `uv run pre-commit run --files <changed>` before committing —
  CI will fail if pre-commit fails.

## Phase 4: Report

Summarize for the user:

- CI: what failed and what was fixed (or noted as pre-existing).
- Copilot: how many comments fixed vs. dismissed, and how many rounds it took.
- Mothership: how many `@sdk-review` rounds it took, how many findings were
  fixed per round, final verdict.
- Any open questions raised via `AskUserQuestion` and how they were resolved.
- The PR URL.

## Constraints

- Only touch code needed to address a CI failure, a Copilot comment, or a
  mothership finding — no drive-by cleanup.
- Never fabricate tool output. If `gh` returns nothing, that means nothing
  happened yet — keep waiting or report the stall, don't invent a result.
- Never skip pre-commit or test hooks (`--no-verify`) unless the user
  explicitly asks.
- Confidentiality: don't put customer/tenant/run-ID identifiers in commit
  messages or replies — reference issues generically.
