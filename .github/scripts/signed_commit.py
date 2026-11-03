"""Publish a locally made commit to a branch as one commit GitHub signs.

The org's ``signed-commits`` ruleset blocks a pull request whose commits are
unsigned. A bot's ``git commit`` on the runner is unsigned, and pushing it with
an App token or a PAT does not change that. A commit created through the GraphQL
``createCommitOnBranch`` mutation is signed by GitHub and shows as Verified,
whatever token made it.

So a caller keeps its local ``git commit`` (that is where the change is built
and checked) and replaces ``git push`` with ``publish`` here (``push`` on the
command line), which sends the same tree change as one signed commit:

- ``expect_head`` publishes only if the branch still points at that commit, the
  ``git push`` fast-forward guarantee: a branch that moved since is left alone
  and the newer run takes over.
- ``reset_to`` + ``lease`` rebuilds the branch as that base plus the one new
  commit, the ``git push --force`` shape of a bot-owned branch regenerated on
  every run. ``lease`` is the sha the branch must still be at
  (``--force-with-lease``), ``absent`` for a branch that must not exist yet (a
  plain push of a new branch), ``tracking`` for whatever the checkout's
  ``refs/remotes/origin/<branch>`` holds (``absent`` when it has none: the
  plain ``--force-with-lease``), or ``any`` for an unconditional ``--force``.

A rebuild never points the real branch at the bare base, even for a moment: a
pull request whose head equals its base has no commits, and GitHub closes it.
The signed commit is made on a short-lived scratch branch at the base, the real
branch is moved straight onto it, and the scratch branch is deleted.

The move is one GraphQL ``updateRefs`` call whose ``beforeOid`` is the lease, so
GitHub applies it only if the branch is still where the lease says, atomically,
as ``--force-with-lease`` does. (The REST refs API has no such condition: a
read-then-PATCH would overwrite a push landing between the two.)

The tree change is read from git (``BASE..HEAD``), so the published commit holds
exactly what the local one does, deletions included. Nothing is published when
there is no change. The API carries file contents only, never a file mode, so a
change that adds or edits an executable, a symlink or a submodule fails here
rather than landing as a plain file.

GitHub's secondary rate limit on content creation (on the order of 80 writes a
minute) can answer any of these writes. A rate-limited request is retried after
the wait GitHub asks for, up to ``RATE_LIMIT_BUDGET_SECONDS`` in all; a
permission denial is never retried.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _gh_refs import (  # noqa: E402
    RateLimited,
    Response,
    gh_request,
    raise_if_rate_limited,
)

ABSENT = "absent"
ANY = "any"
TRACKING = "tracking"

# updateRefs' spelling of "this ref does not exist".
_ZERO_OID = "0" * 40

# The only mode createCommitOnBranch can write.
_PLAIN_FILE = "100644"
_NO_FILE = "000000"

# Scratch branches live under their own prefix, so no workflow or ruleset
# pattern written for real branches matches them.
SCRATCH_PREFIX = "signed-commit-tmp"

# Total time one publish may spend waiting out rate limits before it fails.
RATE_LIMIT_BUDGET_SECONDS = 300
# GitHub's guidance for a secondary limit that names no Retry-After.
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60

_CREATE_COMMIT = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""

_REPOSITORY_ID = """
query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }
"""

_UPDATE_REFS = """
mutation($input: UpdateRefsInput!) { updateRefs(input: $input) { clientMutationId } }
"""


class Api:
    """GitHub requests for one publish: one token, one shared rate-limit budget."""

    def __init__(self, token: str | None = None, sleep=time.sleep) -> None:
        self.token = token
        self.sleep = sleep
        self.waited = 0

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        body_via_file: bool = False,
    ) -> Response:
        while True:
            response = gh_request(
                method,
                path,
                payload,
                body_via_file=body_via_file,
                token=self.token,
            )
            try:
                raise_if_rate_limited(response)
                _raise_if_graphql_rate_limited(response)
            except RateLimited as limited:
                self._wait(limited, f"{method} {path}")
                continue
            return response

    def _wait(self, limited: RateLimited, label: str) -> None:
        wait = limited.retry_after
        if wait is None:
            wait = DEFAULT_RATE_LIMIT_WAIT_SECONDS
        wait = max(wait, 1)
        if self.waited + wait > RATE_LIMIT_BUDGET_SECONDS:
            raise SystemExit(
                f"::error::{label} is still rate-limited after waiting "
                f"{self.waited}s (budget {RATE_LIMIT_BUDGET_SECONDS}s); giving up."
            )
        print(f"::warning::{label} was rate-limited; retrying in {wait}s.")
        self.sleep(wait)
        self.waited += wait


def _raise_if_graphql_rate_limited(response: Response) -> None:
    """GraphQL reports its primary limit as HTTP 200 with a RATE_LIMITED error."""
    body = response.body if isinstance(response.body, dict) else {}
    for error in body.get("errors") or []:
        if isinstance(error, dict) and error.get("type") == "RATE_LIMITED":
            raise RateLimited(None)


def git(*args: str, cwd: Path) -> str:
    """Single seam for git so tests can run against a scratch repo."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def file_changes(base: str, head: str, cwd: Path) -> dict:
    """``fileChanges`` for the tree change ``base..head``, read from git.

    Fails when the change needs a mode the API cannot write: a new or edited
    entry that is not a plain file (executable, symlink, submodule), or a mode
    change. Publishing it would silently turn it into a plain file.
    """
    additions: list[dict] = []
    deletions: list[dict] = []
    unsupported: list[str] = []
    raw = git("diff", "--raw", "--no-renames", "--no-abbrev", "-z", base, head, cwd=cwd)
    fields = raw.split("\0")
    for meta, path in zip(fields[0::2], fields[1::2]):
        if not meta.startswith(":"):
            continue
        old_mode, new_mode, _old_sha, new_sha, status = meta[1:].split()
        if status.startswith("D"):
            deletions.append({"path": path})
            continue
        if new_mode != _PLAIN_FILE or old_mode not in (_NO_FILE, _PLAIN_FILE):
            unsupported.append(f"{path} ({old_mode} -> {new_mode})")
            continue
        blob = subprocess.run(
            ["git", "cat-file", "blob", new_sha],
            cwd=cwd,
            check=True,
            capture_output=True,
        ).stdout
        additions.append({"path": path, "contents": base64.b64encode(blob).decode()})
    if unsupported:
        raise SystemExit(
            "::error::cannot publish as a signed commit: createCommitOnBranch "
            "writes every file as mode 100644, and these entries need another "
            "mode: " + ", ".join(unsupported)
        )
    return {"additions": additions, "deletions": deletions}


def read_branch_head(api: Api, repo: str, branch: str) -> str | None:
    """The sha ``branch`` points at, or None when the branch does not exist."""
    response = api.request("GET", f"repos/{repo}/git/ref/heads/{branch}")
    if response.status == 404:
        return None
    target = response.body.get("object") if isinstance(response.body, dict) else None
    if response.status != 200 or not isinstance(target, dict) or not target.get("sha"):
        raise SystemExit(
            f"::error::could not read refs/heads/{branch} in {repo}: "
            f"HTTP {response.status} {response.message!r}"
        )
    return str(target["sha"])


def check_lease(api: Api, repo: str, branch: str, lease: str) -> str | None:
    """The branch's current sha, refusing if it is not where ``lease`` says.

    An early refusal, before anything is written. It is not what makes the lease
    hold: the final move carries the lease as ``beforeOid``, which GitHub checks
    atomically. ``any`` never refuses.
    """
    current = read_branch_head(api, repo, branch)
    if lease == ANY:
        return current
    expected = None if lease == ABSENT else lease
    if current != expected:
        raise SystemExit(
            f"::error::refs/heads/{branch} moved: expected {lease}, found "
            f"{current or ABSENT}. Another run updated it; not overwriting."
        )
    return current


def create_commit(
    api: Api, repo: str, branch: str, expected_head: str, message: str, changes: dict
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
    data = _graphql(
        api, payload, f"createCommitOnBranch on {branch}", body_via_file=True
    )
    commit = (data.get("createCommitOnBranch") or {}).get("commit")
    if not isinstance(commit, dict) or not commit.get("oid"):
        raise SystemExit(
            f"::error::createCommitOnBranch on {branch} returned no commit."
        )
    return str(commit["oid"])


def _graphql(api: Api, payload: dict, label: str, *, body_via_file=False) -> dict:
    """``data`` of a GraphQL call, failing on an HTTP or GraphQL error."""
    response = api.request("POST", "graphql", payload, body_via_file=body_via_file)
    body = response.body if isinstance(response.body, dict) else {}
    data = body.get("data")
    if response.status != 200 or body.get("errors") or not isinstance(data, dict):
        errors = "; ".join(str(e.get("message")) for e in body.get("errors") or [])
        raise SystemExit(
            f"::error::{label} failed: "
            f"HTTP {response.status} {errors or response.message!r}"
        )
    return data


def _repository_id(api: Api, repo: str) -> str:
    owner, _, name = repo.partition("/")
    data = _graphql(
        api,
        {"query": _REPOSITORY_ID, "variables": {"owner": owner, "name": name}},
        f"reading the id of {repo}",
    )
    repository = data.get("repository")
    if not isinstance(repository, dict) or not repository.get("id"):
        raise SystemExit(f"::error::could not read the id of {repo}.")
    return str(repository["id"])


def move_branch(api: Api, repo: str, branch: str, oid: str, before: str | None) -> None:
    """Point ``branch`` at ``oid``, only if it still points at ``before``.

    ``before`` is a sha, ``_ZERO_OID`` for a branch that must not exist, or None
    for no condition (``--force``). GitHub evaluates the condition and the write
    as one step, so a push landing in between makes this fail, not get lost.
    """
    update: dict = {"name": f"refs/heads/{branch}", "afterOid": oid, "force": True}
    if before is not None:
        update["beforeOid"] = before
    payload = {
        "query": _UPDATE_REFS,
        "variables": {
            "input": {
                "repositoryId": _repository_id(api, repo),
                "refUpdates": [update],
            }
        },
    }
    try:
        _graphql(api, payload, f"updateRefs on refs/heads/{branch}")
    except SystemExit as exc:
        expected = "absent" if before == _ZERO_OID else before
        guard = (
            f" It had to be at {expected}: if it moved, another run updated it; "
            "not overwriting."
            if before is not None
            else ""
        )
        raise SystemExit(
            f"{exc} refs/heads/{branch} was not moved onto {oid}.{guard}"
        ) from None


def _create_ref(api: Api, repo: str, branch: str, sha: str) -> None:
    response = api.request(
        "POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
    )
    if response.status not in (200, 201):
        raise SystemExit(
            f"::error::could not create refs/heads/{branch} at {sha}: "
            f"HTTP {response.status} {response.message!r}"
        )


def rebuild_branch(
    api: Api,
    repo: str,
    branch: str,
    base: str,
    lease: str,
    message: str,
    changes: dict,
) -> str:
    """Point ``branch`` at ``base`` plus one signed commit; return that commit."""
    current = check_lease(api, repo, branch, lease)
    if current is None:
        # Created only if still absent, whatever the lease: an unconditional
        # update of a ref that does not exist is not one updateRefs documents.
        before: str | None = _ZERO_OID
    elif lease == ANY:
        before = None
    else:
        before = current
    scratch = f"{SCRATCH_PREFIX}/{secrets.token_hex(8)}"
    _create_ref(api, repo, scratch, base)
    try:
        oid = create_commit(api, repo, scratch, base, message, changes)
        move_branch(api, repo, branch, oid, before)
    finally:
        response = api.request("DELETE", f"repos/{repo}/git/refs/heads/{scratch}")
        if response.status not in (200, 204):
            print(
                f"::warning::could not delete the scratch branch {scratch} "
                f"(HTTP {response.status}); delete it by hand."
            )
    return oid


def tracking_lease(branch: str, cwd: Path) -> str:
    """What the checkout last saw ``branch`` at: the plain ``--force-with-lease``.

    The remote-tracking ref's sha, or ``absent`` when the checkout has none (the
    branch did not exist when it fetched).
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ABSENT


def publish(
    repo: str,
    branch: str,
    *,
    cwd: Path | str = ".",
    expect_head: str | None = None,
    reset_to: str | None = None,
    lease: str | None = None,
    token: str | None = None,
    sleep=time.sleep,
) -> str | None:
    """Publish local ``HEAD``'s change to ``branch`` as one signed commit.

    Exactly one of ``expect_head`` (fast-forward) or ``reset_to`` with ``lease``
    (rebuild) names the base; either may be any revision git resolves. ``token``
    defaults to ``GH_TOKEN``. Returns the published commit's sha, or None when
    ``HEAD`` changes no files.
    """
    if (expect_head is None) == (reset_to is None):
        raise ValueError("pass exactly one of expect_head or reset_to")
    if reset_to is not None and not lease:
        raise ValueError("reset_to needs a lease")
    cwd = Path(cwd)
    if lease == TRACKING:
        lease = tracking_lease(branch, cwd)
    head = git("rev-parse", "--verify", "HEAD^{commit}", cwd=cwd).strip()
    base_rev = expect_head if expect_head is not None else reset_to
    base = git("rev-parse", "--verify", f"{base_rev}^{{commit}}", cwd=cwd).strip()
    changes = file_changes(base, head, cwd)
    if not changes["additions"] and not changes["deletions"]:
        print(f"Nothing to publish: {base}..{head} changes no files.")
        return None
    message = git("log", "-1", "--format=%B", head, cwd=cwd)
    api = Api(token, sleep)
    if expect_head is not None:
        oid = create_commit(api, repo, branch, base, message, changes)
    else:
        oid = rebuild_branch(api, repo, branch, base, str(lease), message, changes)
    print(f"Published {branch} at {oid} (signed by GitHub).")
    return oid


def push(args: argparse.Namespace) -> int:
    oid = publish(
        args.repo,
        args.branch,
        cwd=args.cwd,
        expect_head=args.expect_head,
        reset_to=args.reset_to,
        lease=args.lease,
    )
    # The published commit is not the local one (GitHub wrote and signed it), so
    # a later step that reads the branch's head needs this sha, not local HEAD.
    output = os.environ.get("GITHUB_OUTPUT")
    if oid and output:
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
        help="Publish on top of this commit, only if the branch still points at it.",
    )
    target.add_argument(
        "--reset-to",
        help="Rebuild the branch as this commit plus the new one.",
    )
    p.add_argument(
        "--lease",
        help=(
            f"With --reset-to: the sha the branch must be at, '{ABSENT}' if it "
            f"must not exist, '{TRACKING}' for the checkout's "
            f"refs/remotes/origin/<branch> (or '{ABSENT}' without one), or "
            f"'{ANY}' to overwrite it unconditionally."
        ),
    )
    p.add_argument("--cwd", default=".", help="The local repository.")
    args = parser.parse_args(argv)
    if args.reset_to and not args.lease:
        parser.error("--reset-to needs --lease")
    return push(args)


if __name__ == "__main__":
    sys.exit(main())
