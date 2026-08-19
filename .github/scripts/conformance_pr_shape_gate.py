#!/usr/bin/env python3
"""Shape gate for conformance-remediation PRs — the deterministic safety boundary.

The remediation rover is told (in ``.mothership/conformance-remediation/CLAUDE.md``)
never to touch ``tests/``, ``.github/`` or ``conformance/``: those are the gates it
is judged against, and a finding "cleared" by editing them would show as a green
dashboard produced by moving the goalposts. That instruction is prose, and prose is
not a boundary. **This file is the boundary.**

Unlike ``vuln_auto_merge_gate.py``, this gate never approves or merges anything —
every remediation PR is human-reviewed by design. It only answers "is this diff the
shape a remediation PR is allowed to have?", as a required check.

Three independent rules, all of which must hold:

  1. **Forbidden paths.** No changed file may sit under a self-judging prefix
     (``tests/``, ``.github/``, ``conformance/``, ``packages/conformance/``).
  2. **Single rule.** The PR addresses exactly one rule ID. A rover that fixed a
     second rule it happened to notice produces an unreviewable diff and breaks the
     one-PR-per-rule accounting the orchestrator's ledger depends on.
  3. **Rule agreement.** The rule ID in the branch name, the PR title, and the
     ``--rule`` claim in the body all name the same rule — a mismatch means the
     PR's own labelling cannot be trusted to describe its contents.

Rule 1 carries a deliberate exception: a rule whose *subject* is a forbidden path
cannot be remediated without touching it. The C-series grades ``.github/``
workflows and the T-series grades ``tests/``; for those, the prefix is the thing
under repair, not the judge. Such rules are listed in ``SUBJECT_PATH_EXEMPT`` and
may write only their own subject prefix — never the others.

Exit 0 = shape is acceptable. Exit 1 = reject, with the reason on stdout as a
GitHub workflow error annotation.

Loop / conditional logic lives here (a tested script) rather than inlined in
workflow YAML, per docs/standards/ci.md.

Environment:
    GH_TOKEN     token with PR read access.
    REPO         owner/name (github.repository).
    PR_NUMBER    the PR to inspect.

Optional:
    CHANGED_FILES  newline-separated paths, bypassing the `gh` call (tests / local).
    PR_TITLE       likewise.
    PR_BRANCH      likewise.
    PR_BODY        likewise.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Callable, Iterable, NamedTuple

#: Prefixes the remediator may never write. These are the gates it is graded by:
#: the conformance suite itself, the test suite the orthogonal gate runs, and the
#: CI that runs both. An edit here can make a finding disappear without the defect
#: being fixed, which is strictly worse than leaving the finding in place.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "tests/",
    ".github/",
    "conformance/",
    "packages/conformance/",
)

#: Rules whose subject IS a normally-forbidden prefix, mapped to the single prefix
#: each may write. A C-series rule grades ``.github/`` workflows; refusing it write
#: access there would make the whole series permanently un-remediable. The exemption
#: is per-prefix, so a C-rule still cannot touch ``tests/``.
SUBJECT_PATH_EXEMPT: dict[str, str] = {
    "C": ".github/",
    "T": "tests/",
}

#: A conformance rule ID: one series letter plus three digits.
RULE_ID_RE = re.compile(r"\b([A-Z]\d{3})\b")

#: ``conformance/l004`` or ``conformance/L004`` — the branch the fleet lane opens.
BRANCH_RULE_RE = re.compile(r"^conformance/([A-Za-z]\d{3})$")


class Verdict(NamedTuple):
    ok: bool
    reason: str


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=60
    ).stdout


def changed_files(
    repo: str, pr: str, runner: Callable[[list[str]], str] = _run
) -> list[str]:
    """Paths changed by the PR, via the API (not a local diff — CI may be shallow)."""
    raw = runner(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr}/files",
            "--jq",
            ".[].filename",
        ]
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def pr_meta(
    repo: str, pr: str, runner: Callable[[list[str]], str] = _run
) -> tuple[str, str, str]:
    """Return ``(title, head_branch, body)`` for the PR."""
    raw = runner(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "title,headRefName,body",
        ]
    )
    obj = json.loads(raw)
    return (
        obj.get("title") or "",
        obj.get("headRefName") or "",
        obj.get("body") or "",
    )


def rule_ids_in(text: str) -> set[str]:
    """Every conformance rule ID mentioned in ``text``."""
    return set(RULE_ID_RE.findall(text or ""))


def declared_rule(title: str, branch: str) -> Verdict:
    """Resolve the single rule this PR claims to address.

    The branch name is authoritative when it follows the fleet lane's convention,
    because it is the key the orchestrator's ledger and the idempotency guard both
    use. The title must agree with it.
    """
    branch_match = BRANCH_RULE_RE.match(branch or "")
    branch_rule = branch_match.group(1).upper() if branch_match else None
    title_rules = rule_ids_in(title)

    if branch_rule:
        if title_rules and branch_rule not in title_rules:
            return Verdict(
                False,
                f"branch names {branch_rule} but the title names "
                f"{sorted(title_rules)} — the PR's own labelling disagrees with itself",
            )
        return Verdict(True, branch_rule)

    # push_to_pr_branch delivery has no conformance/<rule> branch; fall back to
    # the title, which the playbook requires to carry the rule ID.
    if len(title_rules) == 1:
        return Verdict(True, next(iter(title_rules)))
    if not title_rules:
        return Verdict(
            False,
            "no conformance rule ID found in the branch name or PR title — a "
            "remediation PR must say which rule it addresses",
        )
    return Verdict(
        False,
        f"PR title names more than one rule ({sorted(title_rules)}) — one PR "
        "addresses exactly one rule",
    )


def _normalise(path: str) -> str:
    """Strip a leading ``./`` — and only that.

    Not ``lstrip("./")``: ``str.lstrip`` takes a *character set*, so it would turn
    ``.github/workflows/x`` into ``github/workflows/x`` and the ``.github/`` prefix
    would never match. Two of the forbidden prefixes begin with a dot, which is
    exactly where that idiom silently opens the gate.
    """
    while path.startswith("./"):
        path = path[2:]
    return path


def check_paths(files: Iterable[str], rule: str) -> Verdict:
    """Reject any changed file under a forbidden prefix the rule may not write."""
    exempt = SUBJECT_PATH_EXEMPT.get(rule[:1], None)
    offenders: list[str] = []
    for path in files:
        norm = _normalise(path)
        for prefix in FORBIDDEN_PREFIXES:
            if not norm.startswith(prefix):
                continue
            if exempt is not None and norm.startswith(exempt):
                continue  # this rule's subject is that prefix
            offenders.append(path)
            break
    if offenders:
        allowed = f" (rule {rule} may write {exempt} only)" if exempt else ""
        return Verdict(
            False,
            f"changed file(s) under a self-judging path{allowed}: "
            f"{sorted(offenders)}. The remediator may not edit the gates it is "
            "graded by — tests/, .github/ and the conformance suite.",
        )
    return Verdict(True, "")


def check_single_rule(
    files: Iterable[str], title: str, body: str, rule: str
) -> Verdict:
    """The PR must not claim, in title or body, to have fixed other rules.

    Deliberately does NOT scan the diff contents: a fix for one rule legitimately
    mentions others (a suppression comment, a docstring, an adjacent finding note).
    What must be single is the PR's *claim* about what it did, because that is what
    the reviewer and the ledger key off.
    """
    claimed = rule_ids_in(title) | rule_ids_in(body)
    extras = sorted(claimed - {rule})
    if extras:
        return Verdict(
            False,
            f"PR claims to address {extras} in addition to {rule} — one "
            "remediation PR addresses exactly one rule so the diff stays "
            "reviewable and the ledger's one-unit-per-PR accounting holds",
        )
    return Verdict(True, "")


def evaluate(files: list[str], title: str, branch: str, body: str) -> Verdict:
    """Apply all three rules. Returns the first failure, or ok."""
    if not files:
        return Verdict(
            False,
            "PR changes no files — nothing to gate, and an empty remediation PR "
            "should never have been opened",
        )

    resolved = declared_rule(title, branch)
    if not resolved.ok:
        return resolved
    rule = resolved.reason

    for verdict in (
        check_paths(files, rule),
        check_single_rule(files, title, body, rule),
    ):
        if not verdict.ok:
            return verdict

    return Verdict(True, f"shape ok for {rule}: {len(files)} file(s)")


def main() -> int:
    repo = os.environ.get("REPO", "")
    pr = os.environ.get("PR_NUMBER", "")

    # Env overrides let the suite (and a local dry run) drive this with no network.
    env_files = os.environ.get("CHANGED_FILES")
    if env_files is not None:
        files = [line.strip() for line in env_files.splitlines() if line.strip()]
        title = os.environ.get("PR_TITLE", "")
        branch = os.environ.get("PR_BRANCH", "")
        body = os.environ.get("PR_BODY", "")
    else:
        if not repo or not pr:
            print("::error::REPO and PR_NUMBER are required")
            return 1
        try:
            files = changed_files(repo, pr)
            title, branch, body = pr_meta(repo, pr)
        except subprocess.CalledProcessError as e:
            print(f"::error::could not read PR {repo}#{pr}: {e.stderr or e}")
            return 1

    verdict = evaluate(files, title, branch, body)
    if verdict.ok:
        print(verdict.reason)
        return 0
    print(f"::error title=Conformance remediation PR shape::{verdict.reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
