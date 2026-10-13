"""Guard: automation must not add labels to PRs (FND-3832, FND-2201).

Connector ``tests.yaml`` triggers on ``pull_request: labeled`` because that is
the only way the human ``e2e`` label can start a run, and GitHub has no
trigger-level filter for *which* label fired. So every label a bot adds to a PR
is one more Tests run on the same head SHA. The unit job's ``cancel-in-progress``
concurrency group evicts all but one, each evicted run publishes FAILURE on the
required ``tests / Tests Gate`` context, and the PR sits BLOCKED with auto-merge
armed and never enqueued. This has happened twice: Renovate's ``update:<type>``
labels (removed in FND-2201) and the resync lane's ``conformance-resync`` label
(removed in FND-3832). Both were removed and documented, and neither had a test,
so this one exists to stop a third.

Two sweeps:

* :func:`test_no_new_pr_labelling_in_automation` scans every tracked file under
  ``.github/`` and the conformance bootstrap templates (which are scaffolded into
  every connector) for the ways code adds a label: ``gh pr create --label``,
  ``gh pr edit --add-label``, ``POST …/issues/{n}/labels``, the Octokit/GraphQL
  add-labels calls, and label-applying actions. It is textual on purpose, the
  same trade-off as ``test_label_trigger_gates.py``: it proves absence, not
  harm, so a site that is genuinely harmless goes in ``_GRANDFATHERED`` with the
  reason written down, where a reviewer has to read it.
* :func:`test_renovate_configs_apply_no_labels` parses every Renovate config this
  repo owns or scaffolds (the fleet preset, the bootstrap template in both
  automerge modes, the dogfood ``renovate.json``) and fails on any ``labels`` or
  ``addLabels`` value or ``:label``/``:labels`` preset, anywhere in the tree.

Identify bot PRs by head branch + author instead. See "Bot-opened PRs are never
labelled" in ``docs/standards/ci.md``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conformance.bootstrap import render as bootstrap_render  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = "packages/conformance/conformance/bootstrap/templates"
_SWEPT_ROOTS = (".github", _TEMPLATE_DIR, "renovate-config")
_SWEPT_SUFFIXES = {".py", ".yml", ".yaml", ".sh", ".js", ".mjs", ".ts", ".j2"}

#: Each way automation can add a label to a PR, with a canonical example the
#: pattern must match (checked by test_every_pattern_matches_its_example, so a
#: regex edit that stops matching anything fails loudly instead of passing).
_LABEL_ADDS: dict[str, tuple[re.Pattern[str], str]] = {
    "gh --label / --add-label": (
        re.compile(r"(?<![\w-])--(?:add-)?label(?![\w-])"),
        'gh pr create --title t --label "dependencies"',
    ),
    "REST labels[] form field": (
        re.compile(r"labels\[\]="),
        "-f labels[]=conformance-resync",
    ),
    "REST POST/PUT …/labels": (
        # Ends at /labels: a read, an add or a replace. _is_label_write keeps
        # only the writes. DELETE …/labels/<name> is a removal, which fires
        # `unlabeled`, a type tests.yaml does not trigger on.
        re.compile(r"/issues/[^\s/]+/labels(?=[\"'\s`]|$)", re.MULTILINE),
        "gh api -X POST \"repos/{repo}/issues/{pr['number']}/labels\" --input l.json",
    ),
    "Octokit / GraphQL add or set labels": (
        re.compile(r"\b(?:add|set)_?[Ll]abels(?:ToLabelable)?\b"),
        "await github.rest.issues.addLabels({ labels: ['x'] })",
    ),
    "label-applying action": (
        re.compile(r"uses:\s*[\w.-]+/(?:labeler|[\w.-]*add-labels?|stale)@"),
        "uses: actions/labeler@8558fd74291d67161a8a78ce36a881fa63b766a9 # v5",
    ),
}

#: Label-adding sites that predate this guard, as path -> (hits, reason). The
#: count is exact in both directions: one more hit is a new label site and
#: fails; one fewer means a site was removed and the entry must shrink with it,
#: so the allowance cannot outlive the code it was granted for.
_GRANDFATHERED: dict[str, tuple[int, str]] = {
    # ── application-sdk's own PRs. Its PR workflows that take `labeled`
    #    (pull_request.yaml, codeql.yaml) have no cancel-in-progress, so the
    #    extra run is cost, not an evicted failure on a required check. ──
    ".github/scripts/reconcile_allowlist.py": (
        1,
        "vuln-auto-merge label at create, on application-sdk allowlist PRs.",
    ),
    ".github/scripts/vuln_triage/effects.py": (
        1,
        "vuln-auto-merge label at create, on application-sdk vuln-fix PRs.",
    ),
    ".github/scripts/sdk_review_approve.py": (
        4,
        "sdk-review-* verdict labels on application-sdk PRs after review.",
    ),
    ".github/workflows/check-dapr-version.yaml": (
        1,
        "dependencies label at create, on application-sdk Dapr bump PRs.",
    ),
    ".github/workflows/conformance-release.yml": (
        1,
        "release label at create, on application-sdk release PRs.",
    ),
    ".github/workflows/contract-toolkit-release.yml": (
        1,
        "release label at create, on application-sdk release PRs.",
    ),
    ".github/workflows/release.yaml": (
        1,
        "release label at create, on application-sdk release PRs.",
    ),
    # ── connector PRs ──
    ".github/scripts/upsert_release_pr.py": (
        4,
        "release label on connector bump-version-main PRs (via "
        "release-version-bump.yaml). Harmless only because the scaffolded "
        "tests.yaml skips bump-version PRs unless the label is gate-check/e2e; "
        "repos still on an older tests.yaml without that gate do get the "
        "duplicate run. Moving this to branch-only identity is the real fix.",
    ),
    ".github/workflows/release-version-bump.yaml": (
        1,
        "Caller of upsert_release_pr.py; same reasoning.",
    ),
    ".github/workflows/stale.yml": (
        1,
        "actions/stale: debug-only by default, so writes no label unless a "
        "caller opts in. Opting in for PRs would label connector PRs.",
    ),
    ".github/workflows/schedule.yaml": (
        1,
        "actions/stale on application-sdk's own issues/PRs.",
    ),
    ".github/workflow-templates/stale.yml": (
        1,
        "Starter template running actions/stale on the job's GITHUB_TOKEN; "
        "events from GITHUB_TOKEN do not start workflow runs, so its label "
        "adds trigger no Tests run.",
    ),
}


def _tracked_files() -> Iterator[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *_SWEPT_ROOTS],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for rel in out.split("\0"):
        if not rel or "/tests/" in f"/{rel}":
            continue
        path = Path(rel)
        if path.suffix in _SWEPT_SUFFIXES:
            yield path


#: What turns a request to …/issues/{n}/labels into a write: an explicit
#: POST/PUT, or a body (`gh api` switches to POST as soon as a field or
#: `--input` is given, so `-f labels[]=x` alone is a write).
_WRITE_MARKER = re.compile(
    r"\b(?:POST|PUT)\b|labels\[\]=|--input\b|(?:-[fF]|--(?:raw-)?field)[\s\"',]+labels"
)


def _is_label_write(text: str, match: re.Match[str]) -> bool:
    """Whether the request around an ``…/issues/{n}/labels`` path writes.

    Textual, so the request is approximated as the line before the path (where
    a ``-X POST`` sits in a multi-line call) through ~200 characters after it
    (where the method and fields sit in an argv list). That errs towards
    flagging: a read with an unrelated POST a few lines on is reported, and
    goes in _GRANDFATHERED with its reason if it really is a read.
    """
    line_start = text.rfind("\n", 0, match.start())
    window_start = text.rfind("\n", 0, max(line_start, 0)) + 1
    return bool(_WRITE_MARKER.search(text, window_start, match.end() + 200))


_CONFIRM = {"REST POST/PUT …/labels": _is_label_write}


def _hits(text: str) -> list[str]:
    found = []
    for name, (pattern, _) in _LABEL_ADDS.items():
        confirm = _CONFIRM.get(name)
        for match in pattern.finditer(text):
            if confirm and not confirm(text, match):
                continue
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"L{line} {name}: {match.group(0)!r}")
    return found


def _sweep() -> dict[str, list[str]]:
    hits = {}
    for path in _tracked_files():
        found = _hits((_REPO_ROOT / path).read_text(encoding="utf-8"))
        if found:
            hits[path.as_posix()] = found
    return hits


def test_every_pattern_matches_its_example():
    for name, (pattern, example) in _LABEL_ADDS.items():
        assert pattern.search(example), f"{name} no longer matches {example!r}"


@pytest.mark.parametrize(
    "harmless",
    [
        'gh api -X DELETE "repos/${REPO}/issues/${PR_NUMBER}/labels/e2e"',
        'gh api "repos/{repo}/pulls/{pr}" --jq ".labels[].name"',
        "contains(github.event.pull_request.labels.*.name, 'e2e')",
        "--label-filter foo",
        'gh api "repos/$owner/$repo/issues/$pr/labels" --jq ".[].name"',
        'run(["gh", "api", f"repos/{repo}/issues/{n}/labels", "--paginate"])',
    ],
)
def test_removals_and_reads_are_not_flagged(harmless: str):
    assert _hits(harmless) == []


@pytest.mark.parametrize(
    "write",
    [
        'gh api -X POST "repos/$owner/$repo/issues/$pr/labels" --input l.json',
        'gh api --method PUT "repos/o/r/issues/1/labels" -f "labels[]=x"',
        'gh api "repos/o/r/issues/1/labels" -F labels=@labels.json',
        """gh api \\
          -X POST \\
          "repos/${REPO}/issues/${PR_NUMBER}/labels" \\
          --input -""",
    ],
)
def test_rest_label_writes_are_flagged(write: str):
    assert any("REST POST/PUT" in h for h in _hits(write)), write


def test_the_removed_resync_label_post_is_caught():
    # The exact shape FND-3832 removed from conformance_resync.py.
    removed = """
        _run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr['number']}/labels",
                "-X",
                "POST",
                "-f",
                f"labels[]={RESYNC_LABEL}",
            ],
            runner,
        )
    """
    assert len(_hits(removed)) == 2


def test_no_new_pr_labelling_in_automation():
    hits = _sweep()
    problems = []
    for path, found in sorted(hits.items()):
        allowed, _ = _GRANDFATHERED.get(path, (0, ""))
        if len(found) != allowed:
            problems.append(
                f"{path}: {len(found)} label-add site(s), {allowed} grandfathered\n    "
                + "\n    ".join(found)
            )
    for path, (allowed, _) in sorted(_GRANDFATHERED.items()):
        if path not in hits:
            problems.append(f"{path}: grandfathered {allowed} hit(s), now has none")
    assert not problems, (
        "Automation must not add labels to PRs: each label is a `labeled` event, "
        "which starts a duplicate Tests run that can cancel the first and leave a "
        "failed required check (FND-3832). Identify bot PRs by head branch + "
        "author. See docs/standards/ci.md, 'Bot-opened PRs are never labelled'. "
        "If a site removed a label, update its _GRANDFATHERED count.\n\n"
        + "\n".join(problems)
    )


def test_grandfathered_entries_carry_a_reason():
    for path, (count, reason) in _GRANDFATHERED.items():
        assert count > 0 and reason.strip(), path


# ── Renovate ─────────────────────────────────────────────────────────────


def _renovate_configs() -> Iterator[tuple[str, Any]]:
    for rel in ("renovate.json", "renovate-config/default.json"):
        yield rel, json.loads((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    for automerge in ("true", "false"):
        yield (
            f"{_TEMPLATE_DIR}/renovate.json (automerge={automerge})",
            json.loads(bootstrap_render.render("renovate.json", automerge=automerge)),
        )


def _renovate_label_settings(node: Any, where: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{where}.{key}" if where else key
            if key in ("labels", "addLabels") and value:
                yield f"{here} = {value!r}"
            elif key == "extends":
                for preset in value or []:
                    if re.match(r"^:labels?\(", str(preset)):
                        yield f"{here} contains {preset!r}"
            yield from _renovate_label_settings(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _renovate_label_settings(item, f"{where}[{i}]")


def test_renovate_configs_apply_no_labels():
    problems = [
        f"{name}: {finding}"
        for name, config in _renovate_configs()
        for finding in _renovate_label_settings(config)
    ]
    assert not problems, (
        "Renovate must apply no labels to fleet PRs (FND-2201): each one is an "
        "extra `labeled` Tests run whose eviction leaves a failed required check. "
        "Classify Renovate PRs by branch slug (conformance/renovate/classify.py).\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize(
    "config",
    [
        {"labels": ["dependencies"]},
        {"packageRules": [{"matchPackageNames": ["x"], "addLabels": ["sdk"]}]},
        {"extends": ["config:recommended", ":label(renovate)"]},
        {"lockFileMaintenance": {"labels": ["lock"]}},
    ],
)
def test_renovate_label_detector_catches(config: dict):
    assert list(_renovate_label_settings(config))


def test_renovate_label_detector_allows_empty_and_unrelated():
    assert not list(
        _renovate_label_settings({"labels": [], "extends": [":semanticCommits"]})
    )
