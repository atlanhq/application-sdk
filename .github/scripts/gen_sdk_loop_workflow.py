#!/usr/bin/env python3
"""Generate `.github/workflows/sdk-loop.yml` — the unrolled round chain.

The loop is eight review/resolve pairs. Written by hand that is ~700 lines of
near-identical YAML where one mistyped `needs:` in round 6 is invisible in
review and only shows up as a round that silently never runs. So the chain is
generated from one template and the result is committed; `--check` fails CI if
the committed file has drifted, exactly like the rule-catalog docs.

Why unrolled at all, rather than a loop:

* A shell loop inside one job would share a runner, a working tree and an agent
  session between review and resolve. Separate jobs make the isolation
  structural instead of something a future edit can quietly undo.
* A re-dispatch chain would scatter one logical run across many run IDs and
  inherit `workflow_dispatch`'s stale-payload behaviour.

Usage:
    python3 .github/scripts/gen_sdk_loop_workflow.py [--check]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

MAX_ROUNDS = 8
OUT = pathlib.Path(".github/workflows/sdk-loop.yml")

HEADER = """# @sdk-loop — drive a PR to merge-ready without supervision.
#
#   fence -> review 1 -> resolve 1 -> review 2 -> ... -> review 8 -> finalize
#   fence -> review 1 -> finalize                    (workflow_dispatch review_only)
#
# One workflow run, one job per phase, up to 8 review/resolve pairs. The review
# phase runs on xai/grok-4.6 — the same model the existing lanes review with
# — and the resolve phase on gpt-5.6-luna via opencode, both
# through the Atlan AI gateway — a public edge, so no VPN and no mothership
# sandbox. Its URL is a secret and is deliberately not written anywhere in
# this repo. Every phase reads the EXISTING playbooks from this checkout:
# .mothership/pr-review/ORCHESTRATION.md and .mothership/pr-resolve/. Neither
# file is copied or edited by this lane.
#
# This lane is ADDITIVE. @sdk-review and @sdk-resolve are untouched and keep
# working exactly as they do today. All three speak one verdict vocabulary, so
# sdk_review_approve.py, the dedupe, the reconcile and the downgrade/dismiss
# workflows consume a loop verdict with no change.
#
# There is no adversarial review pass. The resolve phase opens by contesting
# the findings it was handed (pr-resolve ORCHESTRATION §3d, "Fix every finding
# or prove it false"), so a second reviewer would be paying twice for one job.
# Contested findings ride a ledger into the next round, which is what lets a
# disagreement converge without a human.
#
# Concurrency, deliberately asymmetric:
#   * Across PRs — none. Different PRs are different branches with no shared
#     mutable state, so loops run fully in parallel. Throttling would make one
#     slow PR delay every other for no correctness gain.
#   * Within one PR — the FIRST run wins. A second @sdk-loop is dismissed by
#     the fence, not queued: a queued run would start against a branch the live
#     loop had already advanced and would be answering a stale request.
#
# A commit landing mid-loop re-aims the loop rather than stopping it: in-flight
# work is discarded, the loop re-baselines on the new head and re-enters
# REVIEW. Nothing is ever fixed against a review of a different sha, and the
# loop never rebases or force-pushes over anyone.
#
# GENERATED FILE — edit .github/scripts/gen_sdk_loop_workflow.py and run it.
# CI staleness: python3 .github/scripts/gen_sdk_loop_workflow.py --check
#
# Required configuration:
#   secrets.LITELLM_API_KEY           gateway key entitled to BOTH xai/grok-4.6
#                                     and gpt-5.6-luna. An unentitled model is a
#                                     fast 400 with real cost, so check first.
#   secrets.FLEET_APP_ID              atlan-app-fleet — already configured for
#   secrets.FLEET_APP_PRIVATE_KEY     the existing lanes. Each phase mints its
#                                     own narrowed installation token from it.
#   secrets.LITELLM_BASE_URL          gateway base URL. Required — there is no
#                                     default and the endpoint is never in-repo.

name: SDK Loop

# The PR number has to be IN the title: the fence matches a live run to a PR
# through `display_title` whenever GitHub leaves `pull_requests` empty, which
# it does for fork runs. Without this, a duplicate loop on a fork PR would not
# be detected and two loops would push to one branch.
run-name: "SDK Loop #${{ github.event.issue.number || inputs.pr }}"

on:
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      pr:
        description: 'PR number to drive'
        required: true
      review_only:
        description: >-
          Review once and stop. Accepts a merged or closed PR, runs no
          resolver, casts no approval. The A/B's instrument: it reviews PRs
          whose human outcome is already known.
        type: boolean
        required: false
        default: false

# Keyed on the PR alone: loops on different PRs never contend. `cancel-in-
# progress: false` because cancelling could kill a resolve phase mid-push —
# the fence dismisses duplicates instead, which is safe at any moment.
concurrency:
  group: sdk-loop-pr-${{ github.event.issue.number || inputs.pr }}
  cancel-in-progress: false

permissions:
  contents: read
  pull-requests: write
  # The fence runs `gh run list` to find a loop already working this PR. An
  # explicit permissions block makes everything unlisted `none`, so without
  # this the duplicate check 403s, the fence raises before it can post, and
  # the lane goes silent — the failure mode it exists to prevent.
  #
  # `write` rather than `read` because a called workflow's permissions cannot
  # exceed its CALLER's: sdk-loop-phase.yml asks for `actions: write` so
  # actions/cache can actually save (see the comment there), and capping it
  # here at `read` would silently keep the cache broken. The fence itself
  # still only reads.
  actions: write
  # Reactions are an Issues-scope resource, so the acknowledgement 403s
  # without this. Caught by test_react_to_comment.py, which asserts every
  # workflow calling the helper grants it — the same Issues-vs-PR distinction
  # the fleet App token needed for posting the verdict comment.
  issues: write

jobs:
  fence:
    name: Fence
    runs-on: ubuntu-latest
    timeout-minutes: 6
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event.issue.pull_request &&
       startsWith(github.event.comment.body, '@sdk-loop'))
    outputs:
      proceed: ${{ steps.fence.outputs.proceed }}
      pr: ${{ steps.fence.outputs.pr }}
      head_ref: ${{ steps.fence.outputs.head_ref }}
      base_sha: ${{ steps.fence.outputs.base_sha }}
      reason: ${{ steps.fence.outputs.reason }}
      # 'true' | 'false', on both triggers. The round gates read THIS, not
      # `inputs.review_only`: `inputs` is empty on an issue_comment run, and a
      # dispatch boolean arrives as a string, so `!inputs.review_only` would be
      # false for the literal 'false'. The fence normalises once; every gate
      # compares against one string.
      review_only: ${{ steps.fence.outputs.review_only }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: '3.14'
      - name: Authorize, dismiss duplicates, pin the baseline
        id: fence
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPO: ${{ github.repository }}
          PR_NUMBER: ${{ github.event.issue.number || inputs.pr }}
          COMMENT_ID: ${{ github.event.comment.id }}
          AUTHOR_ASSOCIATION: ${{ github.event_name == 'workflow_dispatch' && 'OWNER' || github.event.comment.author_association }}
          RUN_ID: ${{ github.run_id }}
          GHA_RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
          WORKFLOW_FILE: sdk-loop.yml
          REVIEW_ONLY: ${{ inputs.review_only }}
        run: python3 .github/scripts/sdk_loop_fence.py

      # The same acknowledgement the other two lanes give: an emoji within
      # seconds, long before any verdict exists, so whoever typed @sdk-loop
      # knows it registered. Runs after the fence so it can reflect the
      # decision, and always() because the script exits 0 by design — a
      # missing emoji must never take down the run it is decorating.
      - name: React to the trigger comment
        if: always() && github.event_name == 'issue_comment'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPO: ${{ github.repository }}
          COMMENT_ID: ${{ github.event.comment.id }}
          REACTION: ${{ steps.fence.outputs.proceed == 'true' && 'eyes' || 'confused' }}
        run: python3 .github/scripts/react_to_comment.py
"""

FOOTER = """
  finalize:
    name: Summary
    needs: [fence, {all_phases}]
    if: always() && needs.fence.outputs.proceed == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 6
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: '3.14'
      - name: Post the run summary
        env:
          GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          REPO: ${{{{ github.repository }}}}
          PR_NUMBER: ${{{{ needs.fence.outputs.pr }}}}
          GHA_RUN_URL: ${{{{ github.server_url }}}}/${{{{ github.repository }}}}/actions/runs/${{{{ github.run_id }}}}
          STOP_REASON: {stop_expr}
          ROUNDS_JSON: {rounds_expr}
        run: python3 .github/scripts/sdk_loop_finalize.py
"""


def prep_job() -> str:
    """Branch and check hygiene, once, before the first review.

    Holds write scope so it can push a MECHANICAL fix — formatting, lint,
    generated drift — for a check the review could only have reported. It
    does not update a behind branch and does not resolve conflicts: both are
    changes to somebody's PR that they did not ask for, and neither is needed
    to review, since the review reads the diff against base. Both are
    reported and left alone.

    Deterministic for a healthy PR — a few `gh` reads and no model at all.

    Skipped on a review-only run, for two reasons that are the same reason:
    it pushes, and its fast track cancels the review. A review-only run's PR
    is usually merged, so a push lands on a branch nobody is driving, and the
    review is the one thing the run exists to produce. Review 1 already falls
    back to the fence's baseline when prep did not run.
    """
    return """
  prep:
    name: Prep
    needs: [fence]
    if: >-
      needs.fence.outputs.proceed == 'true' &&
      needs.fence.outputs.review_only != 'true'
    uses: ./.github/workflows/sdk-loop-phase.yml
    secrets: inherit
    with:
      phase: prep
      round: 0
      pr: ${{ needs.fence.outputs.pr }}
      head_ref: ${{ needs.fence.outputs.head_ref }}
      base_sha: ${{ needs.fence.outputs.base_sha }}
"""


def review_job(n: int) -> str:
    """Review round n.

    Round 1 baselines on the fence. Later rounds baseline on whatever the
    previous pair last observed — the resolve phase's post-push head, or the
    new head a re-aim discovered. Either way the sha handed forward is the one
    the branch is actually on, so a round that starts is never already stale.
    """
    if n == 1:
        # `!cancelled()` rather than a dependency on prep SUCCEEDING: a prep
        # that could not tidy the branch must not cost the review. A branch
        # left behind still reviews correctly.
        # The fast track is the one prep outcome that CANCELS the chain
        # rather than informing it: prep has already re-stamped the previous
        # verdict on the live head, so a review would re-read a diff whose
        # verdict is posted. Every other prep outcome — including a failure —
        # still reviews, which is why this tests for one value rather than
        # requiring success.
        gate = (
            "!cancelled() && needs.fence.outputs.proceed == 'true' && "
            "needs.prep.outputs.outcome != 'fast_track'"
        )
        needs = "[fence, prep]"
        # Fall back to the fence when prep skipped or failed — an empty
        # `new_base_sha` would checkout `ref: ''`.
        base = "${{ needs.prep.outputs.new_base_sha || needs.fence.outputs.base_sha }}"
        # Prep pushes. Without carrying its sha as `ours`, round 1 sees
        # live != baseline with an empty ours-list, calls it `moved_by_other`
        # and re-aims — burning a round every time prep does its job.
        ours = "${{ needs.prep.outputs.pushed_sha }}"
        ledger = "''"
        prior_sha = "''"
        spent = "''"
        reaims = "''"
    else:
        prev_res, prev_rev = f"resolve-{n - 1}", f"review-{n - 1}"
        # Continue when the previous resolve made progress, or when either
        # phase of the previous pair lost the branch and needs a fresh review.
        # `!= 'true'`, not `== 'false'`: a fence from before this output
        # existed emits nothing, and '' must keep looping.
        gate = (
            f"!cancelled() && needs.fence.outputs.proceed == 'true' && "
            f"needs.fence.outputs.review_only != 'true' && ("
            f"needs.{prev_res}.outputs.outcome == 'ok' || "
            f"needs.{prev_res}.outputs.outcome == 'reaim' || "
            f"needs.{prev_rev}.outputs.outcome == 'reaim')"
        )
        needs = f"[fence, {prev_rev}, {prev_res}]"
        base = (
            "${{ needs.%s.outputs.new_base_sha || needs.%s.outputs.new_base_sha }}"
            % (prev_res, prev_rev)
        )
        ours = "${{ needs.%s.outputs.pushed_sha }}" % prev_res
        ledger = "${{ needs.%s.outputs.ledger }}" % prev_res
        prior_sha = "${{ needs.%s.outputs.reviewed_head }}" % prev_rev
        # Whichever of the pair ran last carries the live tally.
        spent = (
            "${{ needs.%s.outputs.spent_total || needs.%s.outputs.spent_total }}"
            % (prev_res, prev_rev)
        )
        reaims = "${{ needs.%s.outputs.reaims }}" % prev_rev
    return f"""
  review-{n}:
    name: Review {n}
    needs: {needs}
    if: >-
      {gate}
    uses: ./.github/workflows/sdk-loop-phase.yml
    secrets: inherit
    with:
      phase: review
      round: {n}
      pr: ${{{{ needs.fence.outputs.pr }}}}
      head_ref: ${{{{ needs.fence.outputs.head_ref }}}}
      base_sha: {base}
      ours: {ours}
      ledger: {ledger}
      prior_sha: {prior_sha}
      spent_so_far: {spent}
      reaims_so_far: {reaims}
      review_only: ${{{{ needs.fence.outputs.review_only || 'false' }}}}
"""


def resolve_job(n: int) -> str:
    """Resolve round n — runs ONLY when the paired review found work.

    `outcome == 'ok'` is exactly NEEDS_FIXES. A clean verdict, a terminal
    verdict, a re-aim and a failure all skip this job, which is what makes
    "resolve only if required" true rather than aspirational.

    A review-only run skips it whatever the verdict: that run exists to
    measure the review, and its PR is usually merged — the one branch the
    resolver must never push to.
    """
    prev = f"review-{n}"
    prev_res = f"resolve-{n - 1}" if n > 1 else None
    # The ledger comes from the PREVIOUS resolve, so that job has to be in
    # `needs` — GitHub rejects the whole workflow at parse time for a
    # `needs.<job>` reference to a job the caller does not depend on, which
    # means no jobs run and the fence never gets to say why. The `!cancelled()`
    # in the gate keeps a skipped previous resolve from blocking this one.
    needs = f"[fence, {prev}]" if prev_res is None else f"[fence, {prev}, {prev_res}]"
    return f"""
  resolve-{n}:
    name: Resolve {n}
    needs: {needs}
    if: >-
      !cancelled() && needs.fence.outputs.review_only != 'true' &&
      needs.{prev}.outputs.outcome == 'ok'
    uses: ./.github/workflows/sdk-loop-phase.yml
    secrets: inherit
    with:
      phase: resolve
      round: {n}
      pr: ${{{{ needs.fence.outputs.pr }}}}
      head_ref: ${{{{ needs.fence.outputs.head_ref }}}}
      base_sha: ${{{{ needs.{prev}.outputs.new_base_sha }}}}
      verdict_url: ${{{{ needs.{prev}.outputs.verdict_url }}}}
      spent_so_far: ${{{{ needs.{prev}.outputs.spent_total }}}}
      ledger: {"${{ needs.%s.outputs.ledger }}" % prev_res if prev_res else "''"}
"""


def _stop_expr() -> str:
    """Pick the stop reason from the last phase that actually reported one.

    Read newest-first so the outcome that ended the run wins over the earlier
    rounds that merely continued it.
    """
    # A fast track ends the run in prep, so no review or resolve outcome
    # exists to describe it. Without this the chain falls through to its
    # 'failed' default and the summary tells the author a phase broke on the
    # run that did exactly what it was supposed to. Guarded to the one value:
    # prep's ordinary outcomes describe branch hygiene, not why a run ended.
    parts = ["needs.prep.outputs.outcome == 'fast_track' && 'fast_track'"]
    # A review-only run has one review outcome and no resolve. Left to the
    # round scan, an 'ok' (NEEDS_FIXES) review would be reported as the reason
    # the run stopped — true of the verdict, false of the run. Name the mode.
    parts.append("needs.fence.outputs.review_only == 'true' && 'review_only'")
    for n in range(MAX_ROUNDS, 0, -1):
        parts.append("needs.resolve-%d.outputs.outcome" % n)
        parts.append("needs.review-%d.outputs.outcome" % n)
    return "${{ " + " || ".join(parts) + " || 'failed' }}"


def _rounds_expr() -> str:
    rows = [
        '{"number":0,"phase":"prep","outcome":"${{ needs.prep.outputs.outcome }}",'
        '"verdict":"","sha":"${{ needs.prep.outputs.new_base_sha }}",'
        '"detail":"${{ needs.prep.outputs.detail }}","cost":"","usage":""}'
    ]
    for n in range(1, MAX_ROUNDS + 1):
        for phase in ("review", "resolve"):
            job = f"{phase}-{n}"
            rows.append(
                '{"number":%d,"phase":"%s","outcome":"${{ needs.%s.outputs.outcome }}",'
                '"verdict":"${{ needs.%s.outputs.verdict }}",'
                '"sha":"${{ needs.%s.outputs.new_base_sha }}",'
                '"detail":"${{ needs.%s.outputs.detail }}",'
                '"cost":"${{ needs.%s.outputs.cost }}",'
                '"usd":"${{ needs.%s.outputs.usd }}",'
                '"usage":"${{ needs.%s.outputs.usage }}"}'
                % (n, phase, job, job, job, job, job, job, job)
            )
    return "'[" + ",".join(rows) + "]'"


def build() -> str:
    body = [HEADER, prep_job()]
    for n in range(1, MAX_ROUNDS + 1):
        body.append(review_job(n))
        body.append(resolve_job(n))
    # `prep` belongs in the Summary's `needs` too, or its row never reaches
    # the round table and a branch update looks like it never happened.
    all_phases = ", ".join(
        ["prep"]
        + [
            f"{phase}-{n}"
            for n in range(1, MAX_ROUNDS + 1)
            for phase in ("review", "resolve")
        ]
    )
    body.append(
        FOOTER.format(
            all_phases=all_phases,
            stop_expr=_stop_expr(),
            rounds_expr=_rounds_expr(),
        )
    )
    return "".join(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    content = build()
    if args.check:
        if not OUT.exists():
            print(f"MISSING: {OUT}", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {OUT}\nRun `python3 {__file__}` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT} is up to date ({MAX_ROUNDS} rounds).")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT} ({MAX_ROUNDS} rounds, {2 * MAX_ROUNDS + 3} jobs)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
