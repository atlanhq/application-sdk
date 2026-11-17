"""Tell the blocking findings a change introduces from the ones it inherited.

A whole-repo run grades the repository: every blocking finding fails it, old
or new. That is the right question for the scheduled run and the dashboard,
and the wrong one for a pull request, where a finding the base branch already
carries is not something the change did. ``detect --since <ref>`` asks the
pull-request question: it fails only on blocking findings the change adds.

How "adds" is decided
---------------------

The suite runs on the working tree as usual. Only when that run has
unsuppressed blocking findings does anything else happen: the merge-base of
``<ref>`` and ``HEAD`` is checked out into a throwaway git worktree, and the
rules that fired are run there too. Findings are then counted per
``(rule, file, discriminator)``, and a key is *introduced* by as many
findings as the working tree has over the base.

Counting, rather than matching findings one to one, is deliberate. A
finding's line moves whenever code above it changes, and its text changes
when the offending line is edited without being fixed, so neither identifies
"the same finding" across two commits. A count needs neither: moving or
rewording a violation leaves it at one, and adding a second makes it two.
The price is that a change which fixes one violation and adds another of the
same rule in the same file nets to zero and passes. That is no regression,
and the scheduled whole-repo run still reports what is left.

Comparing the two trees, rather than filtering findings to changed lines, is
also deliberate: a change can introduce a finding in a file it never touched
(delete a test tree, change a signature another module relies on).

Failure direction
-----------------

If the base cannot be resolved or scanned (a shallow clone, a ref that does
not exist, no git), every blocking finding counts as introduced. An error may
only make the gate stricter, never greener.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from conformance.suite.schema.findings import Finding

#: ``git`` calls here read local objects only; a minute is generous.
_GIT_TIMEOUT_SECONDS = 60

Key = tuple[str, str, str | None]


class BaseUnavailable(Exception):
    """The base tree could not be resolved, checked out, or scanned."""


def _git(root: Path, *args: str) -> str:
    """Run ``git`` in *root* and return stdout; raise ``BaseUnavailable``."""
    if shutil.which("git") is None:
        raise BaseUnavailable("git is not installed")
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaseUnavailable(f"git {args[0]} did not run: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise BaseUnavailable(f"git {args[0]} failed: {detail[0]}")
    return done.stdout


def resolve_base(root: Path, since: str) -> str:
    """Return the commit the change forked from: merge-base of *since* and HEAD."""
    return _git(root, "merge-base", since, "HEAD").strip()


def renamed_paths(root: Path, base_sha: str) -> dict[str, str]:
    """Return ``{path at base: path in the working tree}`` for renamed files.

    Compared against the working tree, not ``HEAD``, so a rename that is not
    committed yet (a local pre-PR run) is still followed.
    """
    out = _git(root, "diff", "--name-status", "-M", "--diff-filter=R", base_sha)
    renames: dict[str, str] = {}
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            renames[fields[1]] = fields[2]
    return renames


@contextmanager
def base_tree(root: Path, base_sha: str) -> Iterator[Path]:
    """Check *base_sha* out into a throwaway worktree and yield its path."""
    parent = Path(tempfile.mkdtemp(prefix="conformance-base-"))
    tree = parent / "tree"
    try:
        _git(root, "worktree", "add", "--detach", "--force", str(tree), base_sha)
        yield tree
    finally:
        try:
            _git(root, "worktree", "remove", "--force", str(tree))
        except BaseUnavailable:
            pass
        shutil.rmtree(parent, ignore_errors=True)
        try:
            _git(root, "worktree", "prune")
        except BaseUnavailable:
            pass


def _key(finding: Finding, renames: dict[str, str] | None = None) -> Key:
    path = finding.file.lstrip("./")
    if renames:
        path = renames.get(path, path)
    return (finding.rule_id, path, finding.discriminator)


def introduced(
    head: list[Finding],
    base: list[Finding],
    renames: dict[str, str] | None = None,
) -> list[Finding]:
    """Return the findings in *head* that *base* does not account for.

    Both lists are taken as given (the caller passes unsuppressed blocking
    findings). For each ``(rule, file, discriminator)`` key the head has
    ``n`` more findings than the base, the LAST ``n`` head findings by line
    are returned: new code is more often appended than prepended, and which
    of several same-rule findings in one file is named does not change the
    verdict.
    """
    budget = Counter(_key(f, renames) for f in base)
    result: list[Finding] = []
    for finding in sorted(head, key=lambda f: (f.file, f.line, f.column)):
        key = _key(finding)
        if budget[key] > 0:
            budget[key] -= 1
        else:
            result.append(finding)
    return result


def introduced_since(
    root: Path,
    since: str,
    head: list[Finding],
    scan: Callable[[Path], list[Finding]],
) -> tuple[list[Finding], str]:
    """Return ``(introduced findings, base sha)`` for the change since *since*.

    *head* is the working tree's unsuppressed blocking findings, and *scan*
    runs the same rules on another tree and returns its unsuppressed blocking
    findings. Raises ``BaseUnavailable`` when the base cannot be compared;
    the caller then treats every finding as introduced.
    """
    base_sha = resolve_base(root, since)
    if not head:
        return [], base_sha
    renames = renamed_paths(root, base_sha)
    with base_tree(root, base_sha) as tree:
        try:
            base = scan(tree)
        except Exception as exc:  # noqa: BLE001 - any scan crash fails strict
            raise BaseUnavailable(f"the base tree could not be scanned: {exc}") from exc
    return introduced(head, base, renames), base_sha
