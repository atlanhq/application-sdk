# Phase 0 steps 6b, 6c and 11b — prior review, delta, and delta scoping

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
