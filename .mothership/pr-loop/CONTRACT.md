# The verdict contract

**Maintainer-facing. No agent reads this file, and no read list may point at
it.** It records what the runner guarantees on the reviewer's behalf, and who
depends on those guarantees.

The rationale for individual decisions lives in the docstrings of
`.github/scripts/sdk_loop_findings.py`, next to the code that implements them —
this repo has been bitten before by rationale living in a second file that then
drifts from the first.

## The comment

`sdk_loop_findings.render_summary()` emits the marker block, in this order:

| Marker | Required | Read by |
|---|---|---|
| `<!-- SDK_REVIEW -->` | always | every consumer, to find the comment at all |
| `<!-- VERDICT: X -->` | always | `sdk_review_approve.py` — labels, the `sdk-review` status, and the `atlan-ci` approval |
| `<!-- REVIEWED_HEAD: <40-hex> -->` | always | the next round, to scope its delta; `sdk_review_approve.py`, to refuse a verdict for a stale sha |
| `<!-- ANSWERS_TRIGGER: <digits> -->` | comment triggers only | the resolver's push guard, to tell this round's verdict from an earlier one landing late |
| `<!-- TOOLKIT_ARTIFACT_HASH: <sha256> -->` | toolkit scopes only | the next round, to carry consumer validation forward |
| `<!-- SDK_LOOP_AB -->` | review-only runs only | `sdk_review_approve.py`, to stand down before reading the verdict; A/B telemetry, to keep measurement rows out of the real ones |

`X` is one of `READY_TO_MERGE`, `NEEDS_FIXES`, `BLOCKED`, `NEEDS_HUMAN`,
`NEEDS_REBASE`. There is no sixth token.

Two rules that are easy to get wrong and cost real debugging when they are:

- **`ANSWERS_TRIGGER` is omitted, never emptied.** `COMMENT_ID` is blank on
  every `workflow_dispatch` run. An empty marker reads as
  present-but-unparseable and can clear a push mid-review, stranding the verdict.
- **`REVIEWED_HEAD` is the reviewed sha, always 40 lowercase hex.** A short sha
  parses but loses the next round's delta base.

## The invariant

`### Findings` empty ⇔ `READY_TO_MERGE`.

This is the resolve loop's termination condition — it fixes until the list is
empty — so it is the one rule in the lane that must not be probabilistic. The
renderer owns it; nothing asks a model to honour it.

It follows that **every tier rendered into `### Findings` blocks the merge**.
That is why `severity.yaml` routes `LOW` and `INFO` to prose instead: a tier
that cannot be actioned would wedge the loop forever.

## Review-only runs

`workflow_dispatch` with `review_only: true` runs the fence and Review 1 and
nothing else. It exists for the A/B, which reviews PRs whose human outcome is
already known — so the PR is usually merged, and every job after Review 1 is
one that changes the branch:

| Job | Full loop | Review-only | Why it must not run |
|---|---|---|---|
| prep | pushes mechanical fixes; fast-tracks an unchanged diff | skipped | a push lands on a branch nobody is driving; a fast track cancels the one review the run exists to produce |
| resolve-*n* | pushes fixes | skipped | the resolver on a merged branch |
| review-*n* ≥ 2 | re-reviews after a resolve | skipped | nothing to re-review |
| approve-on-verdict | labels, `sdk-review` status, `atlan-ci` approval | stands down on `SDK_LOOP_AB` | an approval on a merged PR is noise at best and a false audit trail at worst |

The gates read the **fence's** `review_only` output, never `inputs.review_only`
directly: `inputs` is empty on an `issue_comment` run, and a dispatch boolean
arrives as the string `'false'`, which a bare `if:` reads as true. The fence
normalises once and every gate compares one string. Gates test `!= 'true'` so a
fence from before the output existed keeps looping.

The fence admits a closed or merged PR **only** under `review_only`. The full
loop still refuses one — its resolver pushes.

**The control lane.** `@sdk-review` (mothership) has no state check and would
run on a merged PR via dispatch, but that is the wrong control: its verdict is
authored by `mothership-ai[bot]`, so `approve-on-verdict` would act on it, and
it would review today's rules against a diff from weeks ago. Control rows for
the A/B come from the **review comments already on those PRs** — the review
each PR actually received, at the time, with the rules as they stood. That is
what the human outcome was measured against, so it is the only fair baseline.
Nothing in this lane needs to dispatch the other one.

## Merge authority

Neither lane approves anything. `sdk_review_approve.py` casts the `atlan-ci`
review, and `atlan-ci` is the CODEOWNER whose approval satisfies the ruleset on
`main`. `mothership-ai[bot]` and `atlan-app-fleet[bot]` are Apps and cannot.

So the contract that matters is not "the loop can parse its own output" — it is
"`sdk_review_approve.py` can". `test_verdict_rendering.py` round-trips through
that module's own regexes for exactly this reason.

**Known gap, not fixed here.** `sdk_review_approve.py`'s `VERDICT_AUTHOR` is a
single login, `mothership-ai[bot]`. `sdk_review_reconcile.py` imports it, so the
reconcile safety net is blind to every `@sdk-loop` verdict — those are authored
by `atlan-app-fleet[bot]`. The approve workflow's own `if:` accepts both logins,
so the YAML and the Python disagree. Verified on `main`.

## The payload

The reviewer returns one JSON object and posts nothing. Fields per finding are
the allowlist in `sdk_loop_findings.FINDING_FIELDS`; anything else 422s the
inline-comment handler, so the renderer rejects it rather than stripping it.

`status` and `reviewed_files` are the completion assertion. They exist because
an empty findings list renders `READY_TO_MERGE`: without a positive signal that
a review happened, an agent that crashed after writing its file would
manufacture a merge-ready verdict. `PACK_ID` proves the pack loaded; it does not
prove the work was done.
