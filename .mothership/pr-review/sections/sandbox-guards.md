# Appendix A: Sandbox-only run guards

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
