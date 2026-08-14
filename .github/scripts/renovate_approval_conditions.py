#!/usr/bin/env python3
"""Approval gate for Renovate dependency-only PRs — the atlan-ci code-owner review.

Driver for ``.github/workflows/renovate-auto-approve-reusable.yml``. Every
consumer repo in the fleet calls that reusable at ``@main``, so this file is the
single decision point for what gets an unattended code-owner approval and
therefore for what auto-merges. It is extracted from the workflow's inlined bash
(FND-372) precisely because a wrong condition here does not fail loudly — it
approves something it should not.

An approval is posted iff ALL of the following hold, evaluated in this order and
short-circuiting on the first failure (later conditions cost an API call, so the
order is load-bearing for cost as well as for the log):

  a. author is a Renovate bot — ``atlan-app-fleet[bot]`` (self-hosted fleet
     runner) or ``renovate[bot]`` (Mend; application-sdk itself is still on it)
  b. the PR is open and not a draft
  c. the PR's current HEAD still matches the SHA being evaluated (race guard)
  d. every changed file is dependency-related (see :func:`non_dep_files`)
  e. every ruleset-required check is green (``gh pr checks --required``)
  f. Renovate's own ``renovate/artifacts`` commit status is ``success``
  g. atlan-ci has not already posted an APPROVED review with our signature

**Fail closed.** Every condition withholds approval on anything other than an
affirmative signal. A missing value is never a falsy default that reads as
"fine": an absent ``renovate/artifacts`` context classifies as ``"missing"`` and
blocks, and a metadata call that errors aborts the step rather than proceeding on
a partial view. When adding a condition, make the negative case the default.

**Per-PR isolation.** A commit can be the HEAD of several open PRs. Each is
evaluated independently and a failing *condition* skips only that PR. An API
*error*, by contrast, aborts the whole step with a non-zero exit — that is the
inherited ``set -euo pipefail`` behaviour and it is deliberate: a red step is
visible, and the next workflow_run completion re-evaluates everything anyway.

Environment:
    GH_TOKEN                PAT owned by atlan-ci (repo read + PR write).
    REPO                    owner/name — in a reusable workflow ``github.repository``
                            is the *calling* repo, which is what we want.
    EVENT_NAME              'workflow_run' or 'workflow_dispatch'.
    RUN_SHA                 workflow_run head_sha (empty for dispatch).
    DISPATCH_PR             PR number (workflow_dispatch manual testing).
    EXTRA_DEP_PATTERN       optional ERE alternation of repo-specific dependency
                            paths, appended to the built-in allowlist.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Constants that other automation keys off. Changing any of these is a
# cross-repo change, not a local one.
# ---------------------------------------------------------------------------

#: Leading line of the approval body. This is a STABLE SIGNATURE: the
#: idempotency check (condition g) matches on it, the fleet dashboard scanner
#: (packages/conformance/conformance/renovate/scan.py) reads it, and the
#: @sdk-review workflows deliberately do NOT match it so the two never collide.
APPROVAL_SIGNATURE = "**Renovate auto-approval:**"

#: The login the gate's GH_TOKEN acts as, and whose prior approvals count for
#: idempotency. atlan-ci is a real User account listed in CODEOWNERS — GitHub
#: Apps cannot be code owners, which is why this one path keeps using the PAT.
APPROVER_LOGIN = "atlan-ci"

#: Renovate identities whose PRs are eligible. Keep BOTH: atlan-app-fleet[bot]
#: is the self-hosted fleet runner; renovate[bot] is the Mend-hosted app, which
#: application-sdk itself still uses for its own workflow-action updates. Do not
#: drop renovate[bot] while application-sdk remains on Mend.
RENOVATE_AUTHORS = ("atlan-app-fleet[bot]", "renovate[bot]")

#: Renovate's own artifact-update status context (condition f).
ARTIFACT_CONTEXT = "renovate/artifacts"

#: Sentinel for "no ``renovate/artifacts`` state could be read". Deliberately a
#: value that is not ``success`` rather than an empty string or None, so it
#: prints usefully in the skip log and can never be mistaken for green.
ARTIFACT_MISSING = "missing"

#: Prefix allowlist: anything under .github/ is dependency-related (Renovate
#: pins actions there). Matched as a prefix, not a whole path — subdirectories
#: are in scope, and a lookalike like ``x.github/…`` is not.
DOT_GITHUB_PREFIX = ".github/"

#: Whole-path allowlist, an ERE alternation applied with full-match semantics —
#: the Python equivalent of the workflow's original ``grep -vxE``.
#:
#: Lock/manifest files match at ANY depth via the ``(.*/)?`` prefix, so a
#: monorepo's packages/conformance/uv.lock or apps/*/contract/PklProject ride the
#: gate. That covers everything a Renovate app-contract-toolkit bump touches: the
#: Pkl pin + lock (contract/PklProject, contract/PklProject.deps.json).
#:
#: The regenerated artifacts (app/generated/**, atlan.yaml, app.yaml) are matched
#: ROOT-ONLY, with no ``(.*/)?`` prefix. renovate-pkl-sync only ever writes them
#: at the repo root, so an unrelated app.yaml/atlan.yaml elsewhere in a consumer
#: tree must NOT ride this gate. They are otherwise a deterministic function of
#: the contract and the (already auto-merged) toolkit version.
DEP_FILE_RE = (
    r"(.*/)?uv\.lock"
    r"|(.*/)?package-lock\.json"
    r"|(.*/)?requirements\.txt"
    r"|(.*/)?pyproject\.toml"
    r"|(.*/)?contract/PklProject"
    r"|(.*/)?contract/PklProject\.deps\.json"
    r"|app/generated/.*"
    r"|atlan\.yaml"
    r"|app\.yaml"
)

APPROVAL_BODY = (
    f"{APPROVAL_SIGNATURE} all required CI checks passed.\n"
    "\n"
    "This is an automated code-owner approval posted by `atlan-ci` for a\n"
    "dependency-only Renovate PR. It is automatically dismissed on any new\n"
    "push (`dismiss_stale_reviews_on_push`) and re-posted once the new\n"
    "HEAD's required checks are green."
)

Runner = Callable[..., subprocess.CompletedProcess]


class GhError(RuntimeError):
    """A ``gh`` call whose failure must abort the step rather than be absorbed.

    Used only for the calls the original bash left ungated under ``set -e``:
    the dispatch PR lookup, per-PR metadata, the changed-file listing and the
    review listing. Absorbing these would mean deciding on a partial view of the
    PR — the one thing this gate must never do.
    """


# ---------------------------------------------------------------------------
# Pure conditions. Each returns (ok, message); the message is the ONLY
# observability on why a Renovate PR was not approved, so it always names the
# value that failed, never just the condition.
# ---------------------------------------------------------------------------


def check_author(pr: str, author: str) -> tuple[bool, str]:
    """Condition (a): the PR must come from a Renovate bot."""
    if author not in RENOVATE_AUTHORS:
        return False, f"PR #{pr}: author is '{author}', not a Renovate bot — skipping."
    return True, ""


def check_open(pr: str, state: str, draft: bool) -> tuple[bool, str]:
    """Condition (b): the PR must be open and not a draft."""
    if state != "open" or draft:
        return (
            False,
            f"PR #{pr}: state='{state}' draft='{_json_bool(draft)}' — skipping.",
        )
    return True, ""


def check_head_unchanged(pr: str, head_sha: str, eval_sha: str) -> tuple[bool, str]:
    """Condition (c): race guard — bail if HEAD moved since the event fired.

    Approving a SHA that is no longer HEAD would attach a code-owner review to
    work whose checks were never evaluated. The push that moved HEAD fires its
    own run, so skipping here loses nothing.
    """
    if head_sha != eval_sha:
        return (
            False,
            f"PR #{pr}: HEAD moved ({eval_sha} → {head_sha}). "
            "Skipping — a later run will re-evaluate.",
        )
    return True, ""


def non_dep_files(filenames: list[str], extra_pattern: str = "") -> list[str]:
    """Condition (d): return the changed files that are NOT dependency-related.

    A file is dependency-related iff it sits under ``.github/`` (prefix match) or
    fully matches :data:`DEP_FILE_RE`, optionally extended by the caller's
    ``extra_dep_file_pattern``. An empty list means the PR is dep-only.

    ``extra_pattern`` is appended as a further ERE alternation branch, matching
    the original ``DEP_FILE_RE="${DEP_FILE_RE}|${EXTRA_DEP_PATTERN}"``. Full-match
    semantics come from :func:`re.fullmatch`, standing in for ``grep -vxE``.

    An unparseable ``extra_pattern`` raises ``re.error``, which aborts the step.
    That is a DELIBERATE divergence from the bash this replaces: there the
    invalid-regex ``grep`` exit was swallowed by the trailing ``|| true``, which
    produced an empty non-dep list — i.e. a repo with a typo'd
    ``extra_dep_file_pattern`` would have had *every* changed file approved,
    source files included. Failing the step is the fail-closed direction.
    """
    pattern = DEP_FILE_RE
    if extra_pattern:
        pattern = f"{pattern}|{extra_pattern}"
    compiled = re.compile(pattern)
    return [
        f
        for f in filenames
        if f and not f.startswith(DOT_GITHUB_PREFIX) and not compiled.fullmatch(f)
    ]


def classify_artifact_state(payload: Any) -> str:
    """Condition (f): reduce a combined-status payload to a single state string.

    Returns the state of the FIRST ``renovate/artifacts`` status, or
    :data:`ARTIFACT_MISSING` when the payload carries none, is the wrong shape,
    or could not be fetched at all (``payload is None``).

    Reading "missing" as not-green is only safe because the fleet preset sets
    ``statusCheckWhen.artifactError = "always"``, so the context is published
    green on healthy branches too. Renovate's default publishes it only on error,
    under which every healthy branch would read as missing and stall unapproved —
    see the guard in ``tests/test_renovate_artifact_gate.py``.

    Why this gate exists at all: a failed ``postUpgradeTasks`` command does not
    stop Renovate. It commits whatever its own artifact update produced, raises
    the PR, and still enables platform automerge (``getPlatformPrOptions`` never
    consults ``artifactErrors``; only Renovate's own *branch* automerge is gated
    on them, and this fleet uses ``automergeType=pr``). So the PR would merge on
    the strength of the checks it did pass, with nothing red to show for the part
    that did not happen — a pkl-sync failure landing a toolkit bump whose
    generated artifacts do not match it, or a lock refresh that could not
    resolve. Worse, a command the runner's ``allowedCommands`` allowlist does not
    match is skipped with a log line and nothing else, so "did not run" and "ran
    clean" are indistinguishable without this status.
    """
    if not isinstance(payload, dict):
        return ARTIFACT_MISSING
    statuses = payload.get("statuses")
    if not isinstance(statuses, list):
        return ARTIFACT_MISSING
    for entry in statuses:
        if isinstance(entry, dict) and entry.get("context") == ARTIFACT_CONTEXT:
            state = entry.get("state")
            return state if isinstance(state, str) else ARTIFACT_MISSING
    return ARTIFACT_MISSING


def count_signature_approvals(reviews: list[Any]) -> int:
    """Condition (g): count live atlan-ci approvals bearing our signature.

    Only ``APPROVED`` counts. A push-dismissed review has state ``DISMISSED``, so
    it does not match and a fresh approval is correctly posted on the next green
    run. A human's approval, or an atlan-ci review carrying a different signature
    (``**SDK reviewer's verdict:**``), does not suppress ours.
    """
    return sum(
        1
        for r in reviews
        if isinstance(r, dict)
        and (r.get("user") or {}).get("login") == APPROVER_LOGIN
        and r.get("state") == "APPROVED"
        and str(r.get("body") or "").startswith(APPROVAL_SIGNATURE)
    )


def _json_bool(value: bool) -> str:
    """Render a bool the way the original bash logged it (jq's lowercase form)."""
    return "true" if value else "false"


# ---------------------------------------------------------------------------
# gh I/O. Every call goes through _gh so tests can stub the external tool and
# let the rest of the module run for real (docs/standards/ci.md, "Testability
# seam").
# ---------------------------------------------------------------------------


def _gh(args: list[str], runner: Runner) -> subprocess.CompletedProcess:
    return runner(["gh", *args], check=False, capture_output=True, text=True)


def _gh_json(args: list[str], runner: Runner, *, what: str) -> Any:
    """Run a ``gh api`` call and parse its JSON body, or raise :class:`GhError`.

    Deliberately does NOT pass ``--jq``: shaping the payload here rather than in
    a jq filter keeps every classification decision inside a unit-testable pure
    function. A non-2xx response writes an error body to stdout, so the exit code
    — not the presence of output — is what decides.
    """
    result = _gh(args, runner)
    if result.returncode != 0:
        raise GhError(f"{what}: gh exited {result.returncode}")
    raw = (result.stdout or "").strip()
    if not raw:
        raise GhError(f"{what}: gh returned an empty body")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhError(f"{what}: could not parse gh output as JSON ({exc})") from exc
    return _flatten_pages(payload)


def _flatten_pages(payload: Any) -> Any:
    """Flatten a ``--paginate --slurp`` listing (an array *of page arrays*).

    ``gh api --paginate`` alone emits one JSON document per page, and combining
    it with ``--jq`` applies the filter per page — which is how the original
    bash's ``| length`` review count could emit ``"0\\n0"`` for a two-page PR and
    read as "already approved". ``--slurp`` collapses the pages into one array,
    and this flattens that array one level.
    """
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(p, list) for p in payload)
    ):
        return [item for page in payload for item in page]
    return payload


def resolve_prs(
    repo: str, event_name: str, run_sha: str, dispatch_pr: str, runner: Runner
) -> tuple[list[str], str]:
    """Return ``(pr_numbers, eval_sha)`` for the triggering event.

    ``workflow_dispatch`` evaluates exactly the PR it was given, at that PR's
    current HEAD. ``workflow_run`` evaluates every OPEN PR whose HEAD is the
    completed run's SHA — a single commit can be the HEAD of several.

    The two lookups fail differently, matching the original bash: the dispatch
    lookup raises (a manual re-evaluation of a PR that cannot be read is a
    mistake worth surfacing), while a failed commit→PRs lookup yields no
    candidates and the step exits cleanly. Both approve nothing.
    """
    if event_name == "workflow_dispatch":
        meta = _gh_json(
            ["api", f"repos/{repo}/pulls/{dispatch_pr}"],
            runner,
            what=f"resolving HEAD of dispatched PR #{dispatch_pr}",
        )
        if not isinstance(meta, dict):
            raise GhError(
                f"resolving HEAD of dispatched PR #{dispatch_pr}: unexpected payload shape"
            )
        eval_sha = str((meta.get("head") or {}).get("sha") or "")
        return ([dispatch_pr] if dispatch_pr else []), eval_sha

    try:
        listing = _gh_json(
            ["api", f"repos/{repo}/commits/{run_sha}/pulls"],
            runner,
            what=f"listing PRs for {run_sha}",
        )
    except GhError:
        return [], run_sha
    if not isinstance(listing, list):
        return [], run_sha
    return [
        str(pr["number"])
        for pr in listing
        if isinstance(pr, dict) and pr.get("state") == "open" and pr.get("number")
    ], run_sha


def fetch_pr_meta(repo: str, pr: str, runner: Runner) -> dict[str, Any]:
    payload = _gh_json(
        ["api", f"repos/{repo}/pulls/{pr}"], runner, what=f"fetching PR #{pr}"
    )
    if not isinstance(payload, dict):
        raise GhError(f"fetching PR #{pr}: unexpected payload shape")
    return payload


def fetch_filenames(repo: str, pr: str, runner: Runner) -> list[str]:
    payload = _gh_json(
        ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--slurp"],
        runner,
        what=f"listing changed files for PR #{pr}",
    )
    if not isinstance(payload, list):
        raise GhError(f"listing changed files for PR #{pr}: unexpected payload shape")
    return [f["filename"] for f in payload if isinstance(f, dict) and f.get("filename")]


def required_checks_green(repo: str, pr: str, runner: Runner) -> bool:
    """Condition (e): ``gh pr checks --required`` exits 0 iff every required check
    is passing or skipping.

    Non-required failures are correctly excluded — the ruleset, not this script,
    decides what "required" means, so a repo changing its required contexts needs
    no change here. Pending required checks exit non-zero; a later workflow_run
    completion re-fires and re-evaluates.
    """
    result = _gh(["pr", "checks", pr, "--repo", repo, "--required"], runner)
    # Echo gh's own table so the step log still shows which check was red.
    for stream in (result.stdout, result.stderr):
        if stream and stream.strip():
            print(stream.rstrip())
    return result.returncode == 0


def fetch_artifact_state(repo: str, eval_sha: str, runner: Runner) -> str:
    """Read the ``renovate/artifacts`` state for ``eval_sha``, fail-closed.

    Unlike the metadata calls, an API error here does NOT abort: it classifies as
    :data:`ARTIFACT_MISSING`, which withholds approval just the same. Absorbing
    it is safe precisely because the absorbed value is already the blocking one.
    """
    try:
        payload = _gh_json(
            ["api", f"repos/{repo}/commits/{eval_sha}/status"],
            runner,
            what=f"reading commit status for {eval_sha}",
        )
    except GhError:
        return ARTIFACT_MISSING
    return classify_artifact_state(payload)


def fetch_reviews(repo: str, pr: str, runner: Runner) -> list[Any]:
    payload = _gh_json(
        ["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--slurp"],
        runner,
        what=f"listing reviews for PR #{pr}",
    )
    if not isinstance(payload, list):
        raise GhError(f"listing reviews for PR #{pr}: unexpected payload shape")
    return payload


def approve(repo: str, pr: str, runner: Runner) -> None:
    """Post the atlan-ci code-owner approval. A failure aborts the step."""
    runner(
        [
            "gh",
            "pr",
            "review",
            pr,
            "--repo",
            repo,
            "--approve",
            "--body",
            APPROVAL_BODY,
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def process_pr(
    repo: str, pr: str, eval_sha: str, extra_pattern: str, runner: Runner
) -> bool:
    """Evaluate one PR and approve it if every condition holds.

    Returns True iff a new approval was posted. Conditions are evaluated in the
    documented order and each fetch happens only once the cheaper conditions
    ahead of it have passed, so a non-Renovate PR costs one API call, not six.
    """
    meta = fetch_pr_meta(repo, pr, runner)
    author = str(((meta.get("user") or {}).get("login")) or "")
    state = str(meta.get("state") or "")
    draft = bool(meta.get("draft"))
    head_sha = str(((meta.get("head") or {}).get("sha")) or "")

    for ok, message in (
        check_author(pr, author),
        check_open(pr, state, draft),
        check_head_unchanged(pr, head_sha, eval_sha),
    ):
        if not ok:
            print(message)
            return False

    # d. All changed files must be dependency-related.
    filenames = fetch_filenames(repo, pr, runner)
    if not filenames:
        print(f"PR #{pr}: no changed files found — skipping.")
        return False
    offenders = non_dep_files(filenames, extra_pattern)
    if offenders:
        print(f"PR #{pr}: contains non-dependency files:")
        for name in offenders:
            print(f"  {name}")
        print("Skipping.")
        return False
    print(f"PR #{pr}: all changed files are dependency-related.")

    # e. All ruleset-required checks must be green.
    print(f"PR #{pr}: checking required CI status...")
    if not required_checks_green(repo, pr, runner):
        print(f"PR #{pr}: required checks not yet all green — skipping.")
        return False
    print(f"PR #{pr}: all required checks are green.")

    # f. Renovate's artifact status must be green.
    artifact_state = fetch_artifact_state(repo, eval_sha, runner)
    if artifact_state != "success":
        print(
            f"PR #{pr}: {ARTIFACT_CONTEXT} is '{artifact_state}', "
            "not 'success' — skipping."
        )
        return False
    print(f"PR #{pr}: {ARTIFACT_CONTEXT} is green.")

    # g. Idempotency.
    if count_signature_approvals(fetch_reviews(repo, pr, runner)):
        print(
            f"PR #{pr}: atlan-ci has already approved with the Renovate "
            "signature — skipping."
        )
        return False

    approve(repo, pr, runner)
    print(f"✅ Approved PR #{pr} as atlan-ci (Renovate auto-approval).")
    return True


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ["REPO"]
    event_name = os.environ.get("EVENT_NAME", "workflow_run")
    run_sha = os.environ.get("RUN_SHA", "")
    dispatch_pr = os.environ.get("DISPATCH_PR", "")
    extra_pattern = os.environ.get("EXTRA_DEP_PATTERN", "")

    try:
        pr_numbers, eval_sha = resolve_prs(
            repo, event_name, run_sha, dispatch_pr, runner
        )
        if not pr_numbers:
            print(f"No open PRs found for SHA {eval_sha} — nothing to do.")
            return 0
        for pr in pr_numbers:
            print(f"--- Evaluating PR #{pr} ---")
            process_pr(repo, pr, eval_sha, extra_pattern, runner)
    except GhError as exc:
        # Abort rather than continue on a partial view — the inherited
        # `set -euo pipefail` semantics. A red step is visible; the next
        # workflow_run completion re-evaluates every candidate PR anyway.
        print(f"::error::{exc}")
        return 1
    except re.error as exc:
        # A malformed extra_dep_file_pattern from the caller. Fail the step
        # rather than fall back to a permissive allowlist — see non_dep_files.
        print(
            f"::error::extra_dep_file_pattern is not a valid regular expression "
            f"({exc}). No PR was approved. Fix the pattern in the caller's "
            "`with: extra_dep_file_pattern:`."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
