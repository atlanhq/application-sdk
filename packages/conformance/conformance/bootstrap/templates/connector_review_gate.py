"""Decide whether a PR has been reviewed by the connector review lane.

Reads the PR's reviews as JSON on stdin (what ``gh api
repos/OWNER/REPO/pulls/N/reviews --paginate --slurp`` prints) and exits 0
when the gate passes, 1 when it fails. The caller fetches in a separate,
``continue-on-error`` step and feeds the file in::

    python .github/scripts/connector_review_gate.py \
      --author "$AUTHOR" --policy "$POLICY" --head-sha "$HEAD_SHA" \
      $ENFORCE < reviews.json

Every argument is passed unconditionally so the caller's ``run:`` block
stays straight-line (per docs/standards/ci.md): no ``if``, no ``||``, no
parameter-expansion tricks. All the branching is here, where a test can
reach it, and none of it touches the network.

An EMPTY stdin means the fetch failed, and the gate passes on it — see
``main``.

The signal is the trailer mothership appends to every connector review
body::

    <!-- commit:<40-hex sha> mode:standard -->
    <!-- profile:connector-app -->

Markers are trusted only at the TAIL of the body, mirroring
``harness/core/pr_review/connector_review.py:_review_trailer_sha``.
Everything above the trailer is model- or author-controlled text, so a
PR author can paste a fake marker into a description and it must not
count. The author of the review must also be a known reviewer bot.

``bootstrap`` vendors this file into every consumer repo and overwrites
it on every run, so a consumer cannot fix it locally — the next
bootstrap reverts the fix. It therefore has to be lint-clean under the
strictest ruff config in the fleet, not just this repo's (FND-445).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# The reviewer identities mothership submits connector reviews as. Kept in
# sync with rover-worker's default ``reviewer_bot_logins`` pair; a repo that
# adds a third login there does not need it here, because only the connector
# lane writes the profile marker this gate matches on.
REVIEWER_LOGINS = frozenset(
    {
        "mothership-reviewer[bot]",
        "mothership-ai[bot]",
    },
)

# Authors whose PRs never need a connector review. Renovate raises the bulk
# of fleet PR volume and auto-merges them unattended; requiring a sandbox
# review on every lock bump would both cost real money and stop fleet
# auto-merge dead.
EXEMPT_AUTHORS = frozenset(
    {
        "renovate[bot]",
        "atlan-app-fleet[bot]",
        "dependabot[bot]",
    },
)

# Kept at module level so the line fits inside the tightest wrap in the fleet
# and `ruff format` is a no-op at every width (see FND-445).
NO_REVIEW_HINT = "No connector review yet. Add `ai-connector-review` once CI is green."

PROFILE_MARKER = "<!-- profile:connector-app -->"
LEGACY_PROFILE_FOOTER = "profile: connector-app"
COMMIT_RE = re.compile(r"<!-- commit:([0-9a-f]{40}) mode:standard -->")


def reviewed_sha(body: str) -> str | None:
    """Return the sha a connector review covered, or None if not one.

    Only a trailer that ENDS the body counts, so a marker quoted inside
    the summary text cannot match.
    """
    lines = body.rstrip().split("\n")
    last = lines[-1].strip()

    if last == PROFILE_MARKER and len(lines) >= 2:
        match = COMMIT_RE.fullmatch(lines[-2].strip())
        return match.group(1) if match else None

    # Reviews posted before the machine-readable profile marker existed end
    # with the commit marker and carry the profile in the rendered footer
    # just above it.
    match = COMMIT_RE.fullmatch(last)
    if match and any(LEGACY_PROFILE_FOOTER in line for line in lines[-15:-1]):
        return match.group(1)
    return None


def flatten_pages(payload: object) -> list[dict]:
    """Return a flat review list from either a page or a list of pages.

    ``gh api --paginate --slurp`` wraps each page in an outer list, while a
    single unpaginated call returns the page itself. Accept both so the
    caller never has to care which it got.
    """
    if not isinstance(payload, list):
        return []
    flat: list[dict] = []
    for item in payload:
        if isinstance(item, list):
            flat.extend(entry for entry in item if isinstance(entry, dict))
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def find_review(
    reviews: list[dict],
    head_sha: str | None = None,
) -> tuple[str, str] | None:
    """Return (sha, state) of the newest connector review, else None.

    ``state`` carries the verdict. ``rover-worker``'s ``toReviewEvent``
    maps an APPROVE recommendation to a real GitHub APPROVED review, and
    maps REQUEST_CHANGES / REJECT / CONDITIONAL_APPROVE to COMMENTED. The
    clamp that would turn an APPROVE into a COMMENT applies only when the
    security gate is on, and connector repos run ``security_gate: false``.
    So APPROVED means the reviewer was happy and COMMENTED means it found
    something.

    The NEWEST matching review wins, so a later clean re-review overrides
    an earlier one that requested changes. GitHub returns reviews oldest
    first.

    With ``head_sha`` set, only a review of that exact commit counts
    (strict); without it, any connector review on the PR counts (sticky).
    """
    found: tuple[str, str] | None = None
    for review in reviews:
        login = ((review or {}).get("user") or {}).get("login") or ""
        if login not in REVIEWER_LOGINS:
            continue
        sha = reviewed_sha(str(review.get("body") or ""))
        if sha is None:
            continue
        if head_sha is not None and sha != head_sha:
            continue
        found = (sha, str(review.get("state") or ""))
    return found


def _build_parser() -> argparse.ArgumentParser:
    """Build the gate's argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--author",
        default="",
        help="PR author login; exempt authors pass without a review",
    )
    parser.add_argument(
        "--policy",
        choices=("sticky", "head"),
        default="sticky",
        help="sticky: any connector review counts; head: only one of --head-sha",
    )
    parser.add_argument(
        "--head-sha",
        default="",
        help="the PR's current head; only consulted when --policy head",
    )
    parser.add_argument(
        "--reviewer-config",
        default="",
        help="path to .mothership/reviewer.yaml; absent = gate passes",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="fail on a missing review; without it, only warn",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the gate and return the process exit code."""
    args = _build_parser().parse_args(argv)

    if args.reviewer_config and not pathlib.Path(args.reviewer_config).is_file():
        print(  # noqa: T201
            f"No {args.reviewer_config} - not a connector-review repo, gate passes.",
        )
        return 0

    if args.author in EXEMPT_AUTHORS:
        print(f"Author {args.author} is exempt - gate passes.")  # noqa: T201
        return 0

    # Fail open on anything that means "could not read", as opposed to "read
    # it, there are no reviews". A gate that blocks the fleet on a GitHub
    # outage is worse than one that misses a review. The caller pipes an
    # EMPTY stdin when `gh api` failed, so blank input is that signal — a PR
    # that genuinely has no reviews arrives as `[]` and still fails below.
    raw = sys.stdin.read().strip()
    if not raw:
        print("::warning::Could not read PR reviews - gate passes.")  # noqa: T201
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(  # noqa: T201
            f"::warning::Could not parse PR reviews ({exc}) - gate passes.",
        )
        return 0

    reviews = flatten_pages(payload)
    # Both arguments are always passed, so the caller's shell stays
    # straight-line; the policy decides whether head_sha is consulted.
    wanted = args.head_sha if args.policy == "head" else None
    found = find_review(reviews, wanted or None)

    if found is not None and found[1] == "APPROVED":
        print(f"Connector review approved {found[0][:8]} - gate passes.")  # noqa: T201
        return 0

    # Two different failures, two different things for the author to do.
    # Saying "no review" when the reviewer ran and asked for changes sends
    # them to re-label instead of to the findings.
    if found is None:
        hint = NO_REVIEW_HINT
    else:
        hint = (
            f"The connector reviewer did not approve {found[0][:8]} "
            f"(review state: {found[1]}). Fix the findings on the review, "
            "then add the `ai-connector-review` label again."
        )

    if not args.enforce:
        print(f"::warning::{hint} (not enforced yet)")  # noqa: T201
        return 0
    print(f"::error::{hint}")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())
