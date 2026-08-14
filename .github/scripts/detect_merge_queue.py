#!/usr/bin/env python3
"""Decide whether a PR's base branch is governed by a GitHub merge queue.

``tests-reusable.yaml`` runs the integration tier on the batched state that will
actually land: ``merge_group`` / ``push``, never ``pull_request`` (the unit tier
is the fast per-commit PR signal). That cadence only gates anything when the
repo *has* a merge queue. On a consumer without one, the integration tier ran
solely on ``push`` — i.e. after the merge, where a failure lands red on ``main``
and blocks nobody. A fleet sweep found the majority of ``atlan-*-app`` consumers
in exactly that state.

This script closes the hole. It answers one question — "will this PR be merged
through a queue?" — so the caller can pick the tier that gates *before* the
merge either way:

    merge queue present  ⇒ integration runs in the queue (unchanged cadence)
    merge queue absent   ⇒ integration runs on the pull_request instead

Detection reads repository **rulesets**: a ruleset is authoritative when it is
``enforcement: active``, targets branches, carries a ``merge_queue`` rule, and
its ``conditions.ref_name`` matches the PR's base branch. Matching is per-branch
rather than per-repo on purpose — a repo can queue ``main`` while leaving a
release branch unqueued.

Classic branch protection (``required_merge_queue`` on
``/branches/{branch}/protection``) is deliberately *not* consulted: a sweep of
the ``atlan-*-app`` fleet found no consumer using classic protection at all
(every repo returns 404), so rulesets are the only live mechanism. Should a repo
ever adopt classic protection, this reports "no queue" and integration runs on
the PR — noisier, never less safe.

**Fail-open by design.** Any API error (missing scope, 403, network, malformed
payload) yields ``enabled=false`` ⇒ integration runs on the PR. The direction
matters: the consumers most likely to fail detection — no PAT, no rulesets,
unusual config — are precisely the ones with no merge queue, i.e. the ones this
protects. Failing the other way would silently restore the gap. The measured
cost of a redundant PR-tier run is ~1-2 minutes; a silently ungated integration
suite costs a broken ``main``.

Extracted from inline shell per docs/standards/ci.md (no branching logic in
workflow ``run:`` blocks); unit-tested in tests/test_detect_merge_queue.py.

Environment:
    GH_TOKEN   bearer token for the `gh` CLI. A PAT is preferred (the reusable
               forwards ORG_PAT_GITHUB); the default GITHUB_TOKEN is fine when
               its scope permits reading rulesets, and fails open when it does
               not.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from typing import Callable

RunFn = Callable[[list], str]

# Ruleset targets/enforcements that can gate a branch merge. "evaluate" is
# GitHub's dry-run mode (reports, never blocks) and "disabled" is off, so
# neither actually queues a merge — only "active" counts.
_ACTIVE = "active"
_BRANCH_TARGET = "branch"
_MERGE_QUEUE_RULE = "merge_queue"

# `conditions.ref_name.include` sentinels. GitHub substitutes these rather than
# spelling out a ref, so they must be expanded before pattern matching.
_ALL = "~ALL"
_DEFAULT_BRANCH = "~DEFAULT_BRANCH"

_REFS_HEADS = "refs/heads/"


def _run_gh(args: list) -> str:
    """Run `gh` and return stdout, or "" on any failure.

    Mirrors the seam in discover_org_consumers.py: a single thin wrapper the
    tests stub, with `gh`'s stderr echoed as a ::warning:: so a real auth/scope
    error stays diagnosable in the workflow log instead of collapsing silently
    into the fail-open path.
    """
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        if result.stderr:
            print(
                f"::warning::gh {' '.join(args[:2])} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
        return ""
    return result.stdout


def _normalise(ref: str) -> str:
    """Reduce a ref to its short branch name (``refs/heads/main`` -> ``main``)."""
    return ref[len(_REFS_HEADS) :] if ref.startswith(_REFS_HEADS) else ref


def ref_matches(patterns: list, base_ref: str, default_branch: str) -> bool:
    """Whether ``base_ref`` matches any ruleset ``ref_name`` pattern.

    Patterns are fnmatch globs over the short branch name, plus the two GitHub
    sentinels. ``~ALL`` matches everything; ``~DEFAULT_BRANCH`` matches only the
    repo's default branch.
    """
    target = _normalise(base_ref)
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        if pattern == _ALL:
            return True
        if pattern == _DEFAULT_BRANCH:
            if target == _normalise(default_branch):
                return True
            continue
        if fnmatch.fnmatch(target, _normalise(pattern)):
            return True
    return False


def targets_branch(ruleset: dict, base_ref: str, default_branch: str) -> bool:
    """Whether one *expanded* ruleset is live and applies to ``base_ref``.

    The rule-type-agnostic half of ruleset matching: active enforcement, branch
    target, and ``conditions.ref_name`` covering the branch. Callers add their
    own rule-type test on top — ``governs_branch`` below asks for a merge queue;
    ``gate_enforcement_scan.py`` asks for required status checks and pull
    requests. Kept as one function so the two readers cannot drift on what
    "this ruleset applies here" means.
    """
    if not isinstance(ruleset, dict):
        return False
    if ruleset.get("enforcement") != _ACTIVE:
        return False
    if ruleset.get("target") != _BRANCH_TARGET:
        return False

    ref_name = ((ruleset.get("conditions") or {}).get("ref_name")) or {}
    include = ref_name.get("include") or []
    exclude = ref_name.get("exclude") or []

    # An explicit exclude wins over an include (GitHub evaluates it that way),
    # so a repo that queues every branch *except* a release line reports "no
    # queue" for that line and gets the PR tier instead.
    if ref_matches(exclude, base_ref, default_branch):
        return False
    return ref_matches(include, base_ref, default_branch)


def governs_branch(ruleset: dict, base_ref: str, default_branch: str) -> bool:
    """Whether one *expanded* ruleset puts ``base_ref`` behind a merge queue."""
    if not targets_branch(ruleset, base_ref, default_branch):
        return False

    rules = ruleset.get("rules") or []
    return any(
        isinstance(rule, dict) and rule.get("type") == _MERGE_QUEUE_RULE
        for rule in rules
    )


def _load(raw: str):
    """Parse `gh` JSON output, returning None on empty/invalid payloads.

    A ``--paginate --slurp`` listing is an array *of page arrays*, so those
    entries are flattened one level; anything else (a plain list, a dict error
    body) passes through for the caller to shape-check.
    """
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("::warning::could not parse rulesets payload as JSON", file=sys.stderr)
        return None
    if isinstance(payload, list) and all(isinstance(page, list) for page in payload):
        return [entry for page in payload for entry in page]
    return payload


def detect(
    repo: str,
    base_ref: str,
    default_branch: str,
    run: RunFn | None = None,
) -> bool:
    """Return True iff ``base_ref`` in ``repo`` is behind an active merge queue.

    The list endpoint does not embed rules, so each ruleset is fetched
    individually to inspect its rule types.

    ``run`` is resolved at call time rather than bound as a default argument, so
    stubbing the module-level seam takes effect for callers that don't pass it.
    """
    run = run or _run_gh
    listing = _load(run(["api", f"repos/{repo}/rulesets", "--paginate", "--slurp"]))
    if not isinstance(listing, list):
        # Fail open: no readable ruleset list ⇒ assume no queue ⇒ PR tier runs.
        #
        # Announce it. "Could not read the rulesets" and "this repo has no queue"
        # produce the same return value but mean very different things: the first
        # means a repo that DOES have a queue will also run integration on its
        # PRs. Without this line that difference is invisible in the log.
        print(
            f"::warning::could not read rulesets for {repo} — assuming no merge "
            "queue, so the integration tier will run on this pull_request. If the "
            "repo does have a queue, check the token: reading rulesets needs only "
            "repo read access, so this usually means GH_TOKEN was empty or "
            "scope-limited rather than that the endpoint is admin-gated.",
            file=sys.stderr,
        )
        return False

    for entry in listing:
        if not isinstance(entry, dict):
            continue
        ruleset_id = entry.get("id")
        if ruleset_id is None:
            continue
        detail = _load(run(["api", f"repos/{repo}/rulesets/{ruleset_id}"]))
        if governs_branch(detail, base_ref, default_branch):
            return True
    return False


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect whether a PR base branch is behind a merge queue."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--base-ref",
        required=True,
        help="PR base branch (github.event.pull_request.base.ref)",
    )
    parser.add_argument(
        "--default-branch",
        default="main",
        help="Repo default branch, to expand the ~DEFAULT_BRANCH sentinel.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    enabled = detect(args.repo, args.base_ref, args.default_branch)

    # Surface the decision and its consequence in the log: this is the reason
    # the integration tier does or doesn't appear on the PR, and a reader
    # should not have to re-derive it from job `if:` expressions.
    #
    # MUST go to stderr, not stdout. The caller redirects this script's stdout
    # straight into $GITHUB_OUTPUT, and the runner rejects any line there that
    # has no `=` ("Unable to process file command 'output' successfully"), which
    # would fail the step — and, via the gate's detect-merge-queue rule, redden
    # Tests Gate on every pull_request in every consumer. Workflow commands are
    # honoured on stderr too, so the annotation still renders.
    # stdout is a strict contract: `enabled=<bool>` and nothing else.
    tier = "merge queue" if enabled else "pull_request"
    print(
        f"::notice::merge queue for {args.repo}@{args.base_ref}: "
        f"{'enabled' if enabled else 'not detected'} — integration tier runs on {tier}",
        file=sys.stderr,
    )
    print(f"enabled={'true' if enabled else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
