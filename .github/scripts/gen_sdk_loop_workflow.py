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

# Keyed on the PR alone: loops on different PRs never contend. `cancel-in-
# progress: false` because cancelling could kill a resolve phase mid-push —
# the fence dismisses duplicates instead, which is safe at any moment.
concurrency:
  group: sdk-loop-pr-${{ github.event.issue.number || inputs.pr }}
  cancel-in-progress: false

permissions:
  contents: read
  pull-requests: write

jobs:
  fence:
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
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.12'
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
        run: python3 .github/scripts/sdk_loop_fence.py
"""

FOOTER = """
  finalize:
    needs: [fence, {all_phases}]
    if: always() && needs.fence.outputs.proceed == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 6
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.12'
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


def review_job(n: int) -> str:
    """Review round n.

    Round 1 baselines on the fence. Later rounds baseline on whatever the
    previous pair last observed — the resolve phase's post-push head, or the
    new head a re-aim discovered. Either way the sha handed forward is the one
    the branch is actually on, so a round that starts is never already stale.
    """
    if n == 1:
        gate = "needs.fence.outputs.proceed == 'true'"
        needs = "[fence]"
        base = "${{ needs.fence.outputs.base_sha }}"
        ours = "''"
        ledger = "''"
        prior_sha = "''"
        spent = "''"
    else:
        prev_res, prev_rev = f"resolve-{n - 1}", f"review-{n - 1}"
        # Continue when the previous resolve made progress, or when either
        # phase of the previous pair lost the branch and needs a fresh review.
        gate = (
            f"!cancelled() && needs.fence.outputs.proceed == 'true' && ("
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
    return f"""
  review-{n}:
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
"""


def resolve_job(n: int) -> str:
    """Resolve round n — runs ONLY when the paired review found work.

    `outcome == 'ok'` is exactly NEEDS_FIXES. A clean verdict, a terminal
    verdict, a re-aim and a failure all skip this job, which is what makes
    "resolve only if required" true rather than aspirational.
    """
    prev = f"review-{n}"
    prev_res = f"resolve-{n - 1}" if n > 1 else None
    return f"""
  resolve-{n}:
    needs: [fence, {prev}]
    if: >-
      !cancelled() && needs.{prev}.outputs.outcome == 'ok'
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
    parts = []
    for n in range(MAX_ROUNDS, 0, -1):
        parts.append("needs.resolve-%d.outputs.outcome" % n)
        parts.append("needs.review-%d.outputs.outcome" % n)
    return "${{ " + " || ".join(parts) + " || 'failed' }}"


def _rounds_expr() -> str:
    rows = []
    for n in range(1, MAX_ROUNDS + 1):
        for phase in ("review", "resolve"):
            job = f"{phase}-{n}"
            rows.append(
                '{"number":%d,"phase":"%s","outcome":"${{ needs.%s.outputs.outcome }}",'
                '"verdict":"${{ needs.%s.outputs.verdict }}",'
                '"sha":"${{ needs.%s.outputs.new_base_sha }}",'
                '"detail":"${{ needs.%s.outputs.detail }}",'
                '"cost":"${{ needs.%s.outputs.cost }}"}'
                % (n, phase, job, job, job, job, job)
            )
    return "'[" + ",".join(rows) + "]'"


def build() -> str:
    body = [HEADER]
    for n in range(1, MAX_ROUNDS + 1):
        body.append(review_job(n))
        body.append(resolve_job(n))
    all_phases = ", ".join(
        f"{phase}-{n}"
        for n in range(1, MAX_ROUNDS + 1)
        for phase in ("review", "resolve")
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
    print(f"wrote {OUT} ({MAX_ROUNDS} rounds, {2 * MAX_ROUNDS + 2} jobs)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
