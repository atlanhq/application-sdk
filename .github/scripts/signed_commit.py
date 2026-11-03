"""Publish a locally made commit to a branch as one commit GitHub signs.

The org's ``signed-commits`` ruleset blocks a pull request whose commits are
unsigned. A bot's ``git commit`` on the runner is unsigned, and pushing it with
an App token does not change that. A commit created through the GraphQL
``createCommitOnBranch`` mutation is signed by GitHub and shows as Verified,
whatever token made it.

So a workflow keeps its local ``git commit`` (that is where the change is
built and checked) and replaces ``git push`` with ``push`` here, which sends the
same tree change as one signed commit:

- ``--expect-head SHA`` publishes only if the branch still points at SHA, the
  ``git push`` fast-forward guarantee: a branch that moved since is left alone
  and the newer run takes over.
- ``--reset-to SHA --lease SHA|absent`` first points the branch at SHA, then
  commits on top, the ``git push --force-with-lease`` shape of a bot-owned
  branch that is rebuilt from its base on every run. The reset refuses when the
  branch is no longer where the lease says.

The tree change is read from git (``BASE..HEAD``), so the published commit holds
exactly what the local one does, deletions included. Nothing is published when
there is no change.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _gh_refs import gh_request, raise_if_rate_limited  # noqa: E402

ABSENT = "absent"

_CREATE_COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""


def git(*args: str, cwd: Path) -> str:
    """Single seam for git so tests can run against a scratch repo."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def file_changes(base: str, head: str, cwd: Path) -> dict:
    """``fileChanges`` for the tree change ``base..head``, read from git."""
    additions: list[dict] = []
    deletions: list[dict] = []
    status = git("diff", "--name-status", "--no-renames", "-z", base, head, cwd=cwd)
    fields = status.split("\0")
    for code, path in zip(fields[0::2], fields[1::2]):
        if not code:
            continue
        if code.startswith("D"):
            deletions.append({"path": path})
            continue
        blob = subprocess.run(
            ["git", "show", f"{head}:{path}"], cwd=cwd, check=True, capture_output=True
        ).stdout
        additions.append({"path": path, "contents": base64.b64encode(blob).decode()})
    return {"additions": additions, "deletions": deletions}


def read_branch_head(repo: str, branch: str) -> str | None:
    """The sha ``branch`` points at, or None when the branch does not exist."""
    response = gh_request("GET", f"repos/{repo}/git/ref/heads/{branch}")
    if response.status == 404:
        return None
    raise_if_rate_limited(response)
    target = response.body.get("object") if isinstance(response.body, dict) else None
    if response.status != 200 or not isinstance(target, dict) or not target.get("sha"):
        raise SystemExit(
            f"::error::could not read refs/heads/{branch} in {repo}: "
            f"HTTP {response.status} {response.message!r}"
        )
    return str(target["sha"])


def reset_branch(repo: str, branch: str, to_sha: str, lease: str) -> None:
    """Point ``branch`` at ``to_sha``, refusing if it is not where ``lease`` says.

    The lease is checked by a read just before the write: the REST refs API has
    no compare-and-swap, so a push landing between the two is not caught. The
    commit that follows is guarded by ``expectedHeadOid`` and is.
    """
    current = read_branch_head(repo, branch)
    expected = None if lease == ABSENT else lease
    if current != expected:
        raise SystemExit(
            f"::error::refs/heads/{branch} moved: expected {lease}, found "
            f"{current or ABSENT}. Another run updated it; not overwriting."
        )
    if current is None:
        response = gh_request(
            "POST",
            f"repos/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": to_sha},
        )
    else:
        response = gh_request(
            "PATCH",
            f"repos/{repo}/git/refs/heads/{branch}",
            {"sha": to_sha, "force": True},
        )
    raise_if_rate_limited(response)
    if response.status not in (200, 201):
        raise SystemExit(
            f"::error::could not point refs/heads/{branch} at {to_sha}: "
            f"HTTP {response.status} {response.message!r}"
        )


def create_commit(
    repo: str, branch: str, expected_head: str, message: str, changes: dict
) -> str:
    """Create the signed commit on ``branch`` on top of ``expected_head``."""
    headline, _, body = message.strip().partition("\n")
    msg: dict = {"headline": headline.strip()}
    if body.strip():
        msg["body"] = body.strip()
    payload = {
        "query": _CREATE_COMMIT,
        "variables": {
            "input": {
                "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
                "message": msg,
                "fileChanges": changes,
                "expectedHeadOid": expected_head,
            }
        },
    }
    response = gh_request("POST", "graphql", payload, body_via_file=True)
    raise_if_rate_limited(response)
    body_json = response.body if isinstance(response.body, dict) else {}
    commit = ((body_json.get("data") or {}).get("createCommitOnBranch") or {}).get(
        "commit"
    )
    if response.status != 200 or body_json.get("errors") or not commit:
        errors = "; ".join(str(e.get("message")) for e in body_json.get("errors") or [])
        raise SystemExit(
            f"::error::createCommitOnBranch on {branch} failed: "
            f"HTTP {response.status} {errors or response.message!r}"
        )
    return str(commit["oid"])


def push(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd)
    head = git("rev-parse", "HEAD", cwd=cwd).strip()
    base = args.reset_to or args.expect_head
    changes = file_changes(base, head, cwd)
    if not changes["additions"] and not changes["deletions"]:
        print(f"Nothing to publish: {base}..{head} changes no files.")
        return 0
    message = git("log", "-1", "--format=%B", head, cwd=cwd)
    if args.reset_to:
        reset_branch(args.repo, args.branch, args.reset_to, args.lease)
    oid = create_commit(args.repo, args.branch, base, message, changes)
    print(f"Published {args.branch} at {oid} (signed by GitHub).")
    # The published commit is not the local one (GitHub wrote and signed it), so
    # a later step that reads the branch's head needs this sha, not local HEAD.
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"oid={oid}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("push", help="Publish HEAD's change as one signed commit.")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--branch", required=True, help="Branch name, without refs/heads/.")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--expect-head",
        help="Publish on top of this sha, only if the branch still points at it.",
    )
    target.add_argument(
        "--reset-to", help="Point the branch at this sha first, then commit on top."
    )
    p.add_argument(
        "--lease",
        help=f"With --reset-to: the sha the branch must be at, or '{ABSENT}'.",
    )
    p.add_argument("--cwd", default=".", help="The local repository.")
    args = parser.parse_args(argv)
    if args.reset_to and not args.lease:
        parser.error("--reset-to needs --lease")
    return push(args)


if __name__ == "__main__":
    sys.exit(main())
