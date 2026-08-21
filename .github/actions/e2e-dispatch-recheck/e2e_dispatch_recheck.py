"""Does the SDK commit this connector run was dispatched for still exist?

Why this exists
---------------
``application-sdk``'s ``e2e_dispatch_guard.py`` drops a dispatch whose SHA is no
longer the head of its PR (FND-696). It can only see the window up to the
dispatch. A push landing after that leaves a connector run already in flight for
a commit nobody is going to merge, and that run splits into two cases:

* It has **acquired a tenant lease**. Nothing to be done. Cancelling it is unsafe
  — ``prepare-tenant`` carries ``if: always()`` and finishes installing onto the
  tenants it had leased on its way out — so the queue is the right answer and the
  head commit waits.
* It has **dispatched but not leased yet**: still building the image, running
  unit and integration tests. On the openapi leg of application-sdk PR #3322 that
  was 2m30s (dispatched 21:07:38, leased 21:10:10). It holds nothing, so it can
  stand down for free, and the head commit takes the tenant instead of queueing
  behind a full install-plus-legs cycle.

This closes the second case. It runs immediately before ``lease-tenant`` and its
answer gates the lease, the install and the legs (FND-701).

How it identifies the PR
------------------------
The connector run is handed a SHA (``application_sdk_ref``) and nothing else. It
is NOT told which application-sdk PR that SHA came from, and it cannot work it
out from the SHA alone: ``GET /commits/{sha}/pulls`` returns an EMPTY list for a
commit a force-push has moved past, which is precisely the case that matters.
Verified against the incident SHA itself — ``be82fade`` on PR #3322 is associated
with no pull request, while the head that replaced it resolves normally.

So the PR number is read from the record that authorised the dispatch in the
first place. ``e2e_dispatch_guard.py`` claims ``refs/e2e-dispatch/<app>/<sha>``
before dispatching, pointed at a blob describing the claim; that blob now carries
``pr_number``. Reading it back is a positive identification rather than an
inference, and it costs nothing to keep alive: the guard's prune only deletes a
claim once that SHA's ``Connector E2E run / <app>`` check has settled, which
cannot happen while this run is the thing that has yet to complete it.

A run with no claim ref is therefore not an SDK pull-request dispatch at all —
someone pinning ``application_sdk_ref`` by hand to test a connector against a
particular SDK commit — and is left alone. That is what keeps a manual run from
being silently skipped for a commit that was never a PR head.

Fail-open, in every direction
-----------------------------
Every unreadable answer means "not superseded". The two costs are wildly
asymmetric: standing down wrongly denies a live commit its e2e outright, while
failing to stand down costs the tenant time this was written to save — which is
also exactly the pre-existing behaviour. So this exits 0 always, writes
``superseded=false`` on any doubt, and never fails the job it runs in.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Mirrors e2e_dispatch_guard.REF_NAMESPACE. Duplicated rather than imported: the
# two live in different actions and only a connector repo consumes this one, so
# an import would mean shipping the guard to every connector. A test pins the
# pair equal so they cannot drift apart silently.
REF_NAMESPACE = "e2e-dispatch"


@dataclass(frozen=True)
class Response:
    status: int
    body: object | None


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the HTTP client."""
    return subprocess.run(cmd, **kwargs)


def gh_request(path: str) -> Response:
    """GET the GitHub API, returning the status rather than raising on 4xx.

    curl rather than ``gh api`` for the same reason the lease uses it: ``gh api``
    treats a non-2xx as a command failure and prints its own diagnostic instead
    of the response, and here a 404 is a legitimate answer ("no claim record")
    rather than an error.

    No retry loop, unlike the lease's client. Every failure resolves to "carry on
    as before", so a blip costs a saved lease rather than a run — and a retry
    budget in front of the tenant queue would be paid on the happy path too.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    cmd = [
        "curl",
        "-sS",
        "-i",
        "--max-time",
        "30",
        # Authorization comes in through stdin (-K -) rather than argv, so the
        # token is never visible in /proc/<pid>/cmdline to anything else sharing
        # the runner. Same handling as the lease client.
        "-K",
        "-",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"https://api.github.com/{path}",
    ]
    config = f'header = "Authorization: Bearer {token}"\n' if token else ""
    result = run(cmd, input=config, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"::warning::curl failed for GET {path}: {result.stderr.strip()}")
        return Response(0, None)

    text = result.stdout.replace("\r\n", "\n")
    header_block, _, body = text.partition("\n\n")
    lines = header_block.splitlines()
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        print(f"::warning::could not parse a status line for GET {path}")
        return Response(0, None)
    if not body.strip():
        return Response(status, None)
    try:
        return Response(status, json.loads(body))
    except json.JSONDecodeError:
        # An HTML error page from a proxy must not crash the caller before it can
        # see the status code, which is the part that decides what happens next.
        return Response(status, None)


def claim_ref(app: str, sha: str) -> str:
    """The ref ``e2e_dispatch_guard.claim_ref`` wrote before dispatching this.

    The guard runs the app through its own ``slug()`` before building the ref, and
    this deliberately does not reimplement it. Every rule in that function —
    unsafe-character mapping, ``..`` collapsing, ``.lock`` stripping — exists for
    a free-text workflow input, and none of them can fire on a GitHub repository
    name, whose own character set is already a subset of what slug() permits.
    Copying the rules here would be dead code that could still drift; a test
    instead asserts the two agree on every connector repo name in the fleet.

    Case is the one thing that CAN differ (slug lowercases, a repo name need
    not), so it is handled.
    """
    return f"refs/{REF_NAMESPACE}/{app.strip().lower()}/{sha}"


def claim_blob_sha(repo: str, app: str, sha: str) -> str | None:
    """The blob the dispatch claim for ``(app, sha)`` points at, or None.

    None covers both "no claim exists" (a hand-pinned run, or a claim already
    pruned) and "the claim could not be read". Neither is evidence of anything,
    so both leave the run to proceed.
    """
    ref = claim_ref(app, sha)
    response = gh_request(f"repos/{repo}/git/ref/{ref.removeprefix('refs/')}")
    if response.status == 404:
        print(
            f"No dispatch claim at {ref}, so this run was not dispatched by an "
            "application-sdk pull request. Proceeding."
        )
        return None
    if response.status >= 400 or not isinstance(response.body, dict):
        print(f"::warning::could not read {ref} (HTTP {response.status}). Proceeding.")
        return None
    target = response.body.get("object")
    blob = target.get("sha") if isinstance(target, dict) else None
    return blob if isinstance(blob, str) and blob else None


def claim_pr_number(repo: str, blob_sha: str) -> int | None:
    """The PR number recorded in a dispatch claim, or None if it has none.

    A claim written before the guard started recording ``pr_number`` reads as
    None and is left alone, which is what lets this ship without being in
    lockstep with the guard change that writes the field.
    """
    response = gh_request(f"repos/{repo}/git/blobs/{blob_sha}")
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::could not read the dispatch claim record {blob_sha} "
            f"(HTTP {response.status}). Proceeding."
        )
        return None
    content = response.body.get("content")
    if not isinstance(content, str):
        return None
    try:
        record = json.loads(base64.b64decode(content))
    except (ValueError, json.JSONDecodeError):
        print(f"::warning::the dispatch claim record {blob_sha} is not JSON.")
        return None
    number = record.get("pr_number") if isinstance(record, dict) else None
    # bool is an int in Python, and `pr_number: true` must not read as PR #1.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    return number


def pr_head_sha(repo: str, number: int) -> str | None:
    """The head SHA that PR currently points at, or None if it is unreadable."""
    response = gh_request(f"repos/{repo}/pulls/{number}")
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::could not read {repo}#{number} (HTTP {response.status}). "
            "Proceeding."
        )
        return None
    head = response.body.get("head")
    if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
        return None
    return head["sha"].strip().lower()


def is_superseded(repo: str, app: str, sha: str) -> bool:
    """Has the PR that dispatched ``sha`` moved on to a different head?"""
    blob = claim_blob_sha(repo, app, sha)
    if blob is None:
        return False
    number = claim_pr_number(repo, blob)
    if number is None:
        print(
            "The dispatch claim records no pull request, so this SHA cannot be "
            "compared against a head. Proceeding."
        )
        return False
    head = pr_head_sha(repo, number)
    if head is None or head == sha:
        return False
    print(
        f"::notice::{sha[:8]} is no longer the head of {repo}#{number} "
        f"({head[:8]} is), so this run is testing a commit that has been "
        "superseded. Standing down before the tenant lease rather than making "
        "the live commit queue behind it."
    )
    return True


def write_output(superseded: bool, path: str | None) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"superseded={'true' if superseded else 'false'}\n")


def summarise(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recheck a dispatched SDK ref.")
    parser.add_argument(
        "--sdk-repo",
        default="atlanhq/application-sdk",
        help="The repository the dispatch claim and the pull request live in.",
    )
    parser.add_argument(
        "--sdk-ref",
        required=True,
        help="The application_sdk_ref this run was dispatched with. Anything "
        "that is not a full commit sha disables the check: a branch name has no "
        "fixed head to fall behind.",
    )
    parser.add_argument(
        "--app",
        required=True,
        help="This connector's repository name (e.g. atlan-openapi-app), which "
        "is the app component of the dispatch claim ref.",
    )
    args = parser.parse_args(argv)

    sha = args.sdk_ref.strip().lower()
    app = args.app.strip()
    superseded = False

    if not _SHA_RE.match(sha):
        # Empty on the connector's own pull_request/push runs, and a branch name
        # when someone pins the SDK by branch. Neither names a commit that can be
        # superseded, so there is nothing to ask.
        print(f"--sdk-ref {args.sdk_ref!r} is not a commit sha; nothing to check.")
    elif not app:
        print("::warning::--app is empty, so the dispatch claim cannot be located.")
    else:
        superseded = is_superseded(args.sdk_repo, app, sha)

    if superseded:
        summarise(
            f"- **e2e stood down**: `{sha[:8]}` is no longer the head of the "
            f"`{args.sdk_repo}` pull request that dispatched this run, so the "
            "tenant is left to the commit that is."
        )
    write_output(superseded, os.environ.get("GITHUB_OUTPUT"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
