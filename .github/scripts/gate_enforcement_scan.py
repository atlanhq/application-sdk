#!/usr/bin/env python3
"""Probe whether the Tests Gate is actually *enforced* on each fleet repo, and
emit the result as dashboard data connector-pulse can ingest.

Why this exists (FND-349). Conformance detection reads repo **files**. Whether a
status check is required — and whether it can be bypassed — is not a file; it
lives in GitHub settings. So the one property the "enforce the test gate" work
asserts is the one property the fleet's own drift detector cannot see. Without a
settings probe it would be established once and then trusted forever: a repo can
lose enforcement in one settings page, or be recreated without it, and nothing
anywhere would report it. Silence would read exactly like compliance.

This closes that hole by answering three questions per repo, on the same cadence
as every other fleet finding:

1. Is the gate context a **required** status check on the default branch?
2. Does a **ruleset** require a pull request on that branch? Without one, no
   ruleset stands between a write-access push and the default branch — though
   see the `rulesetRequiresPullRequest` note below for what that does *not*
   establish.
3. Is the check **actually arriving** on pull requests, or configured-but-never-
   reporting? That third state blocks every PR with nothing red to point at, and
   the pressure it creates is to *remove* the requirement — so it has to be
   distinguishable from healthy enforcement rather than lumped in with it.

**Bypass actors are deliberately not reported** — see the STATUS_* block below.
GitHub omits the field entirely for a non-admin caller, so with a fleet-scoped
token "no bypass" and "cannot see bypass" are the same bytes, and the repo reads
as clean either way. An unanswerable question is left unanswered rather than
answered wrongly.

**Nor is "a direct push is permitted"** — schema 3.0 retracted it, for the same
reason one layer down. Rulesets are only one of two enforcement mechanisms;
classic branch protection is the other, it is admin-gated, and this token does
not have admin. Worse, GitHub does not distinguish the two answers: a repo with
no classic protection returns 404 ``"Branch not protected"``, and a repo whose
protection we may not read returns 404 ``"Not Found"``. The first sweep to
publish the claim reported 69/77 repos as permitting direct pushes — which was
exactly the set of repos whose classic protection was unreadable, i.e. a
restatement of the token's own blind spot dressed as a fleet finding. So this
scanner now reports only what rulesets prove
(``enforcement.rulesetRequiresPullRequest``).

The resolution is **not** to widen this token — the fleet App will never hold
``administration: read``. It is to move the state somewhere readable: every
connector app migrates off classic branch protection onto rulesets, which
``/repos/{repo}/rulesets`` returns in full at plain repo-read. That makes the
migration self-measuring, since ``rulesetRequiresPullRequest`` climbing across
the fleet *is* the completion signal, and it needs no new privilege.

Note the direction of the residual error, because it is the forgiving one: an
unreadable classic protection can only make this scanner *understate* how
protected a repo is (it reports "no ruleset requires a PR" on a repo that classic
protection may well be gating). It can never overstate it. The retracted field
was therefore a false alarm rather than a false green — which is still worth
retracting, but is the opposite of the ``bypass_actors`` failure above.

Enumerating the fleet here (rather than having each repo self-report) is the
whole point: a repo that has lost enforcement is *present in the output* with
``gated: false``, instead of quietly dropping out of a dashboard built from
whoever happened to publish. Absence of a repo means discovery failed, and that
is itself reported.

Reads, never writes. ``/repos/{repo}/rulesets`` is not administration-gated —
``detect_merge_queue.py`` has relied on exactly that in every consumer's CI — so
the ruleset path needs no new privilege. Every field this scanner reads comes
from ``rules``, which that same plain repo-read returns in full.

**Fail-loud, unlike detect_merge_queue.py.** That script fails open because a
misdetection there costs a redundant test run. Here a misdetection would report
an ungated repo as gated — the exact false-green this issue exists to prevent —
so every unreadable repo lands in the output as ``status: "unknown"`` with the
error attached, and the fleet rollup counts it separately from ``not-gated``.
Nothing is inferred from a failed read.

Output mirrors the Renovate fleet dashboard layout (``repos/<slug>.json`` +
``fleet.json`` + append-only ``history_*.jsonl``, camelCase on the wire) so the
existing Kryptonite deploy shape applies unchanged and connector-pulse sees one
serialization convention across all three fleet artifacts.

Extracted per docs/standards/ci.md (no branching logic in workflow ``run:``
blocks); unit-tested in tests/test_gate_enforcement_scan.py.

Environment:
    GH_TOKEN   bearer token for the `gh` CLI. Needs repo read across the fleet
               (the atlan-app-fleet App installation token is enough).

Usage:
    gate_enforcement_scan.py --owner atlanhq --out /tmp/gate-enforcement
    gate_enforcement_scan.py --repos-file repos.json --out /tmp/gate-enforcement
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))

from detect_merge_queue import targets_branch  # noqa: E402

RunFn = Callable[[list], str]

# The check the milestone requires. `tests` is the *caller job id* in each
# consumer's tests.yaml, `Tests Gate` the job name inside the reusable — GitHub
# composes the context from those two and does NOT include the workflow name.
# Getting this wrong reads as "no repo is gated", so it is asserted against a
# real ruleset payload in the tests rather than trusted from documentation.
DEFAULT_REQUIRED_CONTEXT = "tests / Tests Gate"

# Fleet membership is the atlan-*-app naming convention, matching
# discover_org_consumers.py. Deliberately not filtered by "extends the Renovate
# preset" the way that script is: a repo being off Renovate has no bearing on
# whether its gate is enforced, and filtering on it would shrink the denominator
# to flatter the headline number.
DEFAULT_NAME_PATTERN = r"^atlan-[a-z0-9-]+-app$"

# See discover_org_consumers.REPO_LIST_LIMIT — same cap, same loud warning when
# the org grows into it, because a silently truncated fleet would understate the
# ungated count.
REPO_LIST_LIMIT = 5000

_REQUIRED_STATUS_CHECKS_RULE = "required_status_checks"
_PULL_REQUEST_RULE = "pull_request"

SCHEMA_VERSION = "3.0"

# Per-repo verdict. Three values, not a boolean, because "we could not read it"
# must never collapse into "it is not gated" (that would make an outage look
# like a regression) nor into "it is gated" (a false green).
#
# Deliberately NOT reporting bypassability. GitHub *omits* the `bypass_actors`
# key entirely — not an empty list, not a 403 — for any caller without admin on
# the repo, while still returning `rules`. The fleet App token holds contents +
# pull-requests read by design, so absent and none are indistinguishable to it,
# and a repo with standing admin bypass reads as clean. That is the precise
# false green this scanner exists to prevent, so the claim is not made at all
# rather than made unreliably. The same test retired `directPushPermitted` in
# schema 3.0: see the module docstring. What is left is
# `enforcement.rulesetRequiresPullRequest`, which claims only what `rules`
# actually shows and names the mechanism it is scoped to.
STATUS_GATED = "gated"
STATUS_NOT_GATED = "not-gated"
STATUS_UNKNOWN = "unknown"

# Arrival verdicts for the configured-but-never-reporting state.
ARRIVAL_REPORTING = "reporting"
ARRIVAL_INTERMITTENT = "intermittent"
ARRIVAL_NEVER = "never-arriving"
ARRIVAL_NO_DATA = "no-data"
ARRIVAL_UNKNOWN = "unknown"

# Finding ids. Stable strings — connector-pulse groups on them.
FINDING_NOT_REQUIRED = "gate-not-required"
# `gate-direct-push-permitted` was removed in schema 3.0 along with the field it
# reported on. Not reused for anything else — connector-pulse groups findings by
# these strings, and recycling a retired id would silently merge two different
# claims in any stored snapshot's `byFinding` rollup.
FINDING_NOT_ARRIVING = "gate-not-arriving"
FINDING_UNPRODUCIBLE = "gate-context-unproducible"
FINDING_UNREADABLE = "gate-state-unreadable"

# The SDK's standard location for the workflow that produces the gate context.
#
# Its absence is a *hint*, never a verdict. GitHub composes the context from the
# caller job id and the reusable's job name, so any workflow file with a `tests`
# job calling tests-reusable.yaml emits `tests / Tests Gate` — and repos in this
# fleet do exactly that from differently-named files. Observed arrival on real
# pull requests is the authoritative signal; this path only corroborates it.
TESTS_WORKFLOW_PATH = ".github/workflows/tests.yaml"


# ---------------------------------------------------------------------------
# API seam
# ---------------------------------------------------------------------------


class GhError(RuntimeError):
    """A `gh` invocation failed. Carries the HTTP status when one is parseable.

    Raised rather than swallowed: this scanner's whole value is that an
    unreadable repo is reported as unreadable instead of guessed at.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


_STATUS_RE = re.compile(r"HTTP (\d{3})")


def _run_gh(args: list) -> str:
    """Run `gh` and return stdout; raise GhError with gh's stderr on failure.

    The single seam the tests stub, mirroring detect_merge_queue.py and
    discover_org_consumers.py — but raising where those return "", because here
    "no data" and "an error" must not produce the same record.
    """
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        match = _STATUS_RE.search(stderr)
        raise GhError(
            f"gh {' '.join(args[:2])} failed: {stderr or 'no stderr'}",
            status=int(match.group(1)) if match else None,
        )
    return result.stdout


def _load_json(raw: str):
    """Parse `gh` JSON output. Returns None for empty/invalid payloads.

    A ``--paginate --slurp`` listing is an array *of page arrays*, so those are
    flattened one level; anything else passes through for the caller to
    shape-check.
    """
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(p, list) for p in payload)
    ):
        return [entry for page in payload for entry in page]
    return payload


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def required_contexts(ruleset: dict) -> list:
    """Every status-check context a ruleset requires, in payload order."""
    contexts: list = []
    for rule in ruleset.get("rules") or []:
        if (
            not isinstance(rule, dict)
            or rule.get("type") != _REQUIRED_STATUS_CHECKS_RULE
        ):
            continue
        params = rule.get("parameters") or {}
        for check in params.get("required_status_checks") or []:
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                contexts.append(check["context"])
    return contexts


def has_rule(ruleset: dict, rule_type: str) -> bool:
    return any(
        isinstance(rule, dict) and rule.get("type") == rule_type
        for rule in ruleset.get("rules") or []
    )


def strict_policy(ruleset: dict) -> bool:
    """Whether required checks must be re-run against an up-to-date base.

    Reported but not scored: with a merge queue it is redundant, without one it
    is the difference between "the gate passed on this code" and "the gate
    passed on this code six merges ago".
    """
    for rule in ruleset.get("rules") or []:
        if (
            not isinstance(rule, dict)
            or rule.get("type") != _REQUIRED_STATUS_CHECKS_RULE
        ):
            continue
        if (rule.get("parameters") or {}).get("strict_required_status_checks_policy"):
            return True
    return False


def classify_arrival(samples: list) -> tuple:
    """Reduce per-PR context sightings to an arrival verdict.

    ``samples`` is a list of ``{"found": bool, "truncated": bool}``. Presence is
    the whole question — a PR shows several cancelled gate runs plus one live
    one on the same SHA (the concurrency-group behaviour on bot PRs), so
    *conclusions* are noise here and only *appearance* is signal. That also
    sidesteps the check-runs ordering trap entirely.

    A truncated sample (>100 contexts on the commit, gate not among the first
    100) proves nothing either way, so it is excluded from the denominator
    rather than counted as a miss.
    """
    conclusive = [s for s in samples if s.get("found") or not s.get("truncated")]
    truncated = len(samples) - len(conclusive)
    if not conclusive:
        return (
            ARRIVAL_UNKNOWN if truncated else ARRIVAL_NO_DATA,
            0,
            0,
            truncated,
        )
    found = sum(1 for s in conclusive if s.get("found"))
    if found == len(conclusive):
        verdict = ARRIVAL_REPORTING
    elif found == 0:
        verdict = ARRIVAL_NEVER
    else:
        verdict = ARRIVAL_INTERMITTENT
    return verdict, len(conclusive), found, truncated


def _finding(finding_id: str, severity: str, message: str) -> dict:
    return {"id": finding_id, "severity": severity, "message": message}


def evaluate_repo(
    repo: str,
    default_branch: str,
    rulesets: list,
    arrival_samples: Optional[list],
    has_tests_workflow_file: Optional[bool],
    required_context: str,
    errors: list,
) -> dict:
    """Build one repo's record from already-fetched payloads. No I/O.

    ``rulesets`` are *expanded* ruleset objects (the list endpoint omits rules,
    so each must be fetched individually before it reaches here).
    """
    collected_at = _now()

    if errors:
        # An unreadable repo gets a record, not a guess. Everything downstream
        # keys on `status`, and `unknown` is counted apart from `not-gated` in
        # the rollup so an auth outage can never be misread as mass regression.
        return {
            "schemaVersion": SCHEMA_VERSION,
            "repo": repo,
            "collectedAt": collected_at,
            "defaultBranch": default_branch,
            "requiredContext": required_context,
            "gated": None,
            "status": STATUS_UNKNOWN,
            "enforcement": None,
            "arrival": {
                "status": ARRIVAL_UNKNOWN,
                "prsSampled": 0,
                "prsWithContext": 0,
                "truncatedSamples": 0,
            },
            "hasTestsWorkflowFile": has_tests_workflow_file,
            "findings": [
                _finding(
                    FINDING_UNREADABLE,
                    "error",
                    "Gate enforcement state could not be read: " + "; ".join(errors),
                )
            ],
            "errors": list(errors),
        }

    governing = [
        rs for rs in rulesets if targets_branch(rs, default_branch, default_branch)
    ]

    all_contexts: list = []
    matched_rulesets: list = []
    requires_pr = False
    strict = False
    for ruleset in governing:
        contexts = required_contexts(ruleset)
        if required_context in contexts:
            matched_rulesets.append(
                {
                    "id": ruleset.get("id"),
                    "name": ruleset.get("name"),
                    "sourceType": ruleset.get("source_type"),
                }
            )
            strict = strict or strict_policy(ruleset)
        all_contexts.extend(contexts)
        requires_pr = requires_pr or has_rule(ruleset, _PULL_REQUEST_RULE)

    gated = bool(matched_rulesets)

    arrival_status, sampled, with_context, truncated = classify_arrival(
        arrival_samples or []
    )

    findings: list = []
    if not gated:
        findings.append(
            _finding(
                FINDING_NOT_REQUIRED,
                "error",
                f"{required_context!r} is not a required status check on "
                f"{default_branch}"
                + (
                    f" (required there: {', '.join(sorted(set(all_contexts)))})"
                    if all_contexts
                    else " (no ruleset requires any status check on this branch)"
                ),
            )
        )
    else:
        if arrival_status in (ARRIVAL_NEVER, ARRIVAL_INTERMITTENT):
            findings.append(
                _finding(
                    FINDING_NOT_ARRIVING,
                    "error",
                    f"required but observed on only {with_context}/{sampled} recent "
                    "pull requests — a required context that never reports blocks "
                    "every PR and creates pressure to drop the requirement",
                )
            )
        if arrival_status == ARRIVAL_NEVER and has_tests_workflow_file is False:
            # The file corroborates the empirical miss; it does not stand in for
            # it. A repo can produce this context from a differently-named
            # workflow, so absence alone would be a false positive.
            findings.append(
                _finding(
                    FINDING_UNPRODUCIBLE,
                    "error",
                    f"never observed on a pull request and no {TESTS_WORKFLOW_PATH} "
                    "exists to produce it — every pull request will block with "
                    "nothing red to fix",
                )
            )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "repo": repo,
        "collectedAt": collected_at,
        "defaultBranch": default_branch,
        "requiredContext": required_context,
        "gated": gated,
        "status": STATUS_GATED if gated else STATUS_NOT_GATED,
        "enforcement": {
            "required": gated,
            "source": "ruleset" if gated else "none",
            "rulesets": matched_rulesets,
            "requiredContexts": sorted(set(all_contexts)),
            # Scoped to rulesets in the name, because rulesets are the only
            # mechanism this token can read. False means "no ruleset requires a
            # PR here" — NOT "a direct push is permitted": classic branch
            # protection could be requiring one invisibly. See the module
            # docstring for why the stronger claim was retracted in 3.0.
            "rulesetRequiresPullRequest": requires_pr,
            "strictRequiredStatusChecksPolicy": strict,
        },
        "arrival": {
            "status": arrival_status,
            "prsSampled": sampled,
            "prsWithContext": with_context,
            "truncatedSamples": truncated,
        },
        "hasTestsWorkflowFile": has_tests_workflow_file,
        "findings": findings,
        "errors": [],
    }


def build_fleet(records: list, required_context: str) -> dict:
    """Roll per-repo records up into the fleet aggregate.

    The headline binary — how many repos are gated — is answerable from this one
    object, which is the point: producing it must not require a manual sweep or
    a fan-out read of every per-repo file.
    """
    by_status: dict = {}
    by_finding: dict = {}
    by_arrival: dict = {}
    for record in records:
        by_status[record["status"]] = by_status.get(record["status"], 0) + 1
        arrival = (record.get("arrival") or {}).get("status", ARRIVAL_UNKNOWN)
        by_arrival[arrival] = by_arrival.get(arrival, 0) + 1
        for finding in record.get("findings") or []:
            by_finding[finding["id"]] = by_finding.get(finding["id"], 0) + 1

    gated = by_status.get(STATUS_GATED, 0)
    unknown = by_status.get(STATUS_UNKNOWN, 0)
    # Denominator excludes unreadable repos: a percentage that silently absorbs
    # an auth outage is exactly the reassuring-but-meaningless number this issue
    # is about. `unknown` is carried alongside so the gap is visible.
    known = len(records) - unknown

    return {
        "schemaVersion": SCHEMA_VERSION,
        "collectedAt": _now(),
        "requiredContext": required_context,
        "fleetSize": len(records),
        "gated": gated,
        "notGated": by_status.get(STATUS_NOT_GATED, 0),
        "unknown": unknown,
        "gatedPct": round(100.0 * gated / known, 1) if known else 0.0,
        "withTestsWorkflowFile": sum(
            1 for r in records if r.get("hasTestsWorkflowFile")
        ),
        # Counted the way it reads: how many repos a ruleset requires a PR on.
        # The old `directPushPermitted` counted the inverse and called it a
        # bypass count, which turned "no ruleset PR rule" into a claim about
        # classic branch protection that this token cannot see.
        "rulesetRequiresPullRequest": sum(
            1
            for r in records
            if (r.get("enforcement") or {}).get("rulesetRequiresPullRequest")
        ),
        "byStatus": by_status,
        "byArrival": by_arrival,
        "byFinding": by_finding,
        # Inlined so the binary count and its per-repo breakdown are one fetch.
        "repos": [
            {"repo": r["repo"], "status": r["status"], "gated": r["gated"]}
            for r in records
        ],
    }


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_fleet_repos(owner: str, name_pattern: str, run: RunFn = _run_gh) -> list:
    """Non-archived ``owner`` repos whose bare name matches ``name_pattern``.

    ``gh repo list`` rather than ``gh search code``: code search is best-effort
    and nondeterministic (see discover_org_consumers.py), and a dropped repo
    here silently removes an ungated repo from the count.
    """
    raw = run(
        [
            "repo",
            "list",
            owner,
            "--no-archived",
            "--limit",
            str(REPO_LIST_LIMIT),
            "--json",
            "nameWithOwner",
            "--jq",
            "[.[].nameWithOwner]",
        ]
    )
    repos = _load_json(raw) or []
    if not isinstance(repos, list):
        raise GhError(f"unexpected `gh repo list` payload for {owner}")
    if len(repos) >= REPO_LIST_LIMIT:
        print(
            f"::warning::gh repo list returned {len(repos)} repos, hitting the "
            f"--limit {REPO_LIST_LIMIT} cap; the fleet may be truncated and an "
            "ungated repo silently dropped. Raise REPO_LIST_LIMIT.",
            file=sys.stderr,
        )
    pattern = re.compile(name_pattern)
    return sorted(r for r in repos if pattern.match(str(r).split("/", 1)[-1]))


def fetch_default_branch(repo: str, run: RunFn = _run_gh) -> str:
    payload = _load_json(run(["api", f"repos/{repo}", "--jq", "{b: .default_branch}"]))
    branch = (payload or {}).get("b") if isinstance(payload, dict) else None
    if not branch:
        raise GhError(f"could not read default branch for {repo}")
    return branch


def fetch_rulesets(repo: str, run: RunFn = _run_gh) -> list:
    """Every active ruleset applying to ``repo``, expanded to include its rules.

    ``includes_parents`` is GitHub's default and is passed explicitly: if
    enforcement ever lands as one org-level ruleset instead of 166 repo-level
    ones, it arrives through this same call with ``source_type: Organization``
    and nothing below needs to change.
    """
    listing = _load_json(
        run(
            [
                "api",
                f"repos/{repo}/rulesets?includes_parents=true",
                "--paginate",
                "--slurp",
            ]
        )
    )
    if not isinstance(listing, list):
        raise GhError(f"unexpected rulesets payload for {repo}")
    expanded: list = []
    for entry in listing:
        if not isinstance(entry, dict) or entry.get("id") is None:
            raise GhError(f"malformed ruleset list entry for {repo}: {entry!r}")
        detail = _load_json(run(["api", f"repos/{repo}/rulesets/{entry['id']}"]))
        if not isinstance(detail, dict):
            # A failed or malformed detail read must not read as "this ruleset
            # does not exist": evaluating the repo on a partially-expanded list
            # is the false green this scanner exists to prevent. Propagate so
            # scan_repo records the repo `unknown`.
            raise GhError(
                f"could not expand ruleset {entry['id']} for {repo}: "
                "detail fetch failed or returned a non-object"
            )
        expanded.append(detail)
    return expanded


# Classic branch protection is deliberately not consulted. It is admin-gated,
# so this token gets a 403 on every repo and could never distinguish "no
# protection" from "cannot see it" — and a sweep of the fleet found no consumer
# using it at all (every repo 404s), which detect_merge_queue.py documents from
# the same finding. Rulesets are the only live mechanism.


def fetch_has_tests_workflow_file(repo: str, run: RunFn = _run_gh) -> bool:
    try:
        run(["api", f"repos/{repo}/contents/{TESTS_WORKFLOW_PATH}", "--jq", ".sha"])
    except GhError as exc:
        if exc.status == 404:
            return False
        raise
    return True


_ARRIVAL_QUERY = """
query($owner: String!, $name: String!, $base: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, orderBy: {field: UPDATED_AT, direction: DESC},
                 baseRefName: $base) {
      nodes {
        number
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                contexts(first: 100) {
                  totalCount
                  nodes {
                    __typename
                    ... on CheckRun { name }
                    ... on StatusContext { context }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _expect_object(value, path: str, *, required: bool = False) -> Optional[dict]:
    """The value at ``path`` as a dict, or ``None`` for a JSON null.

    GraphQL returns every field it was asked for, so a *null* is the schema's
    own "nothing here" and a legitimate skip at the nested levels (a pull
    request with no commits, a commit with no checks). ``required=True`` is for
    the spine of the response, where a null means the read failed.

    A present-but-wrong-typed value is schema drift, and it must raise here.
    Walking it with ``(value or {}).get(...)`` instead raises ``AttributeError``
    deep in the chain — which ``scan_repo`` does not catch, so one malformed
    body would abort the entire fleet sweep rather than marking that one repo
    ``unknown``. That is the same false-green-by-abort the fail-loud contract
    exists to prevent, one level down.
    """
    if value is None:
        if required:
            raise GhError(f"malformed arrival payload: {path} is null")
        return None
    if not isinstance(value, dict):
        raise GhError(
            f"malformed arrival payload: expected {path} to be an object, "
            f"got {type(value).__name__}"
        )
    return value


def _expect_list(value, path: str, *, required: bool = False) -> list:
    """The value at ``path`` as a list; ``[]`` for a JSON null. See _expect_object."""
    if value is None:
        if required:
            raise GhError(f"malformed arrival payload: {path} is null")
        return []
    if not isinstance(value, list):
        raise GhError(
            f"malformed arrival payload: expected {path} to be a list, "
            f"got {type(value).__name__}"
        )
    return value


def parse_arrival_nodes(payload: dict, required_context: str) -> list:
    """Turn a GraphQL arrival response into ``{found, truncated}`` samples.

    One query per repo rather than one per pull request: the fleet sweep is
    already O(repos) on the REST side, and fanning arrival out per PR would
    multiply that by the sample size for no extra signal.

    Every level is shape-checked on the way down, so "malformed" reaches the
    caller as a ``GhError`` — and therefore as arrival ``unknown`` for that one
    repo — structurally, rather than depending on each level remembering to
    guard itself.
    """
    root = _expect_object(payload, "response", required=True)
    data = _expect_object(root.get("data"), "data", required=True)
    repository = _expect_object(
        data.get("repository"), "data.repository", required=True
    )
    pull_requests = _expect_object(
        repository.get("pullRequests"), "data.repository.pullRequests", required=True
    )
    nodes = _expect_list(
        pull_requests.get("nodes"),
        "data.repository.pullRequests.nodes",
        required=True,
    )

    samples: list = []
    for index, node in enumerate(nodes):
        where = f"pullRequests.nodes[{index}]"
        pr = _expect_object(node, where)
        if pr is None:
            continue

        commits = _expect_object(pr.get("commits"), f"{where}.commits")
        commit_nodes = _expect_list(
            commits.get("nodes") if commits else None, f"{where}.commits.nodes"
        )
        if not commit_nodes:
            continue

        head = _expect_object(commit_nodes[0], f"{where}.commits.nodes[0]")
        commit = _expect_object(
            head.get("commit") if head else None, f"{where}.commits.nodes[0].commit"
        )
        rollup = _expect_object(
            commit.get("statusCheckRollup") if commit else None,
            f"{where}.statusCheckRollup",
        )
        if rollup is None:
            # No checks ran on the head commit at all — carries no information
            # about whether the gate would have arrived.
            continue

        contexts = _expect_object(rollup.get("contexts"), f"{where}.contexts")
        context_nodes = _expect_list(
            contexts.get("nodes") if contexts else None, f"{where}.contexts.nodes"
        )
        names = set()
        for ctx_index, ctx_node in enumerate(context_nodes):
            ctx = _expect_object(ctx_node, f"{where}.contexts.nodes[{ctx_index}]")
            if ctx is None:
                continue
            # CheckRun exposes `name`, StatusContext exposes `context`. Select
            # by *presence*, not truthiness: an `or`-chain would collapse a
            # present-but-falsy leaf (`0`, `False`, `[]`, `{}`) to the fallback
            # or to `None`, skipping it as "absent" — a wrong-typed leaf then
            # silently reads as `found: False`, the false clean negative the
            # fail-loud contract forbids.
            name = ctx.get("name")
            if name is None:
                name = ctx.get("context")
            if name is None:
                continue
            if not isinstance(name, str):
                # The leaf analogue of the container guards above: a present-
                # but-wrong-typed `name`/`context` is schema drift, and must
                # reach the caller as GhError rather than aborting the sweep
                # (an unhashable list/dict raises an uncaught TypeError in
                # `names.add`, which `scan_repo` does not catch) or silently
                # misclassifying (a hashable int/bool never matches the
                # required-context string, reading as a false `found: False`).
                raise GhError(
                    f"malformed arrival payload: expected "
                    f"{where}.contexts.nodes[{ctx_index}].name/context to be a "
                    f"string, got {type(name).__name__}"
                )
            names.add(name)

        total = (contexts or {}).get("totalCount")
        if total is None:
            total = 0
        if not isinstance(total, int) or isinstance(total, bool):
            raise GhError(
                f"malformed arrival payload: expected {where}.contexts.totalCount "
                f"to be an integer, got {type(total).__name__}"
            )

        samples.append(
            {
                "number": pr.get("number"),
                "found": required_context in names,
                # Compare against the nodes actually returned, NOT the distinct
                # names: a commit routinely carries the same context several
                # times (a bot PR stacks 5-7 gate runs on one SHA, all but the
                # newest cancelled), so the deduplicated set is smaller than
                # totalCount even when nothing was truncated. Measuring against
                # the set marked most busy repos truncated, which quietly
                # converted real never-arriving evidence into `unknown`.
                "truncated": total > len(context_nodes),
            }
        )
    return samples


def fetch_arrival_samples(
    repo: str,
    default_branch: str,
    sample_size: int,
    required_context: str,
    run: RunFn = _run_gh,
) -> list:
    owner, _, name = repo.partition("/")
    raw = run(
        [
            "api",
            "graphql",
            "-f",
            f"query={_ARRIVAL_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"base={default_branch}",
            "-F",
            f"first={sample_size}",
        ]
    )
    payload = _load_json(raw)
    if not isinstance(payload, dict):
        raise GhError(f"unexpected arrival payload for {repo}")
    if payload.get("errors"):
        raise GhError(f"GraphQL errors for {repo}: {payload['errors']}")
    return parse_arrival_nodes(payload, required_context)


def scan_repo(
    repo: str,
    required_context: str,
    sample_size: int,
    run: RunFn = _run_gh,
) -> dict:
    """Collect every input for one repo, then evaluate it.

    The two reads that establish `gated` — default branch and rulesets — are
    fatal: failing either sends the repo to `unknown` rather than letting it be
    evaluated on partial evidence. The corroborating reads below are guarded
    individually, so one unreadable facet degrades that field alone.
    """
    errors: list = []
    default_branch = "main"
    rulesets: list = []
    has_tests: Optional[bool] = None
    samples: Optional[list] = None

    try:
        default_branch = fetch_default_branch(repo, run=run)
    except GhError as exc:
        errors.append(str(exc))

    if not errors:
        try:
            rulesets = fetch_rulesets(repo, run=run)
        except GhError as exc:
            errors.append(str(exc))

    if not errors:
        try:
            has_tests = fetch_has_tests_workflow_file(repo, run=run)
        except GhError as exc:
            print(f"::warning::{repo}: {exc}", file=sys.stderr)
        if sample_size > 0:
            try:
                samples = fetch_arrival_samples(
                    repo, default_branch, sample_size, required_context, run=run
                )
            except GhError as exc:
                print(
                    f"::warning::{repo}: arrival probe failed: {exc}", file=sys.stderr
                )

    return evaluate_repo(
        repo=repo,
        default_branch=default_branch,
        rulesets=rulesets,
        arrival_samples=samples,
        has_tests_workflow_file=has_tests,
        required_context=required_context,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def slug_for(repo: str) -> str:
    return repo.replace("/", "_")


def write_outputs(records: list, fleet: dict, out_dir: Path) -> None:
    """Write ``repos/<slug>.json``, ``fleet.json`` and append-only history.

    Layout is identical to the Renovate fleet dashboard so the existing
    Kryptonite deploy step applies with only the S3 prefix changed.
    """
    (out_dir / "repos").mkdir(parents=True, exist_ok=True)
    for record in records:
        slug = slug_for(record["repo"])
        (out_dir / "repos" / f"{slug}.json").write_text(json.dumps(record, indent=2))
        entry = {
            "date": record["collectedAt"][:10],
            "repo": record["repo"],
            "status": record["status"],
            "gated": record["gated"],
            "arrival": (record.get("arrival") or {}).get("status"),
        }
        with (out_dir / f"history_{slug}.jsonl").open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    (out_dir / "fleet.json").write_text(json.dumps(fleet, indent=2))
    fleet_entry = {
        "date": fleet["collectedAt"][:10],
        "fleetSize": fleet["fleetSize"],
        "gated": fleet["gated"],
        "notGated": fleet["notGated"],
        "unknown": fleet["unknown"],
        "rulesetRequiresPullRequest": fleet["rulesetRequiresPullRequest"],
    }
    with (out_dir / "history_fleet.jsonl").open("a") as fh:
        fh.write(json.dumps(fleet_entry) + "\n")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--owner", default="atlanhq", help="org to enumerate")
    parser.add_argument(
        "--name-pattern",
        default=DEFAULT_NAME_PATTERN,
        help=f"regex the bare repo name must match (default: {DEFAULT_NAME_PATTERN})",
    )
    parser.add_argument(
        "--repos-file",
        type=Path,
        default=None,
        help="JSON array of repo full names to scan instead of discovering them",
    )
    parser.add_argument(
        "--required-context",
        default=DEFAULT_REQUIRED_CONTEXT,
        help=f"the status check that must be required (default: {DEFAULT_REQUIRED_CONTEXT!r})",
    )
    parser.add_argument(
        "--pr-sample",
        type=int,
        default=5,
        help="recent pull requests per repo to probe for check arrival; 0 disables",
    )
    parser.add_argument("--out", type=Path, default=Path("/tmp/gate-enforcement"))
    args = parser.parse_args(argv)

    if args.repos_file:
        repos = json.loads(args.repos_file.read_text())
    else:
        repos = list_fleet_repos(args.owner, args.name_pattern)

    if not repos:
        print(
            "::error::No fleet repos discovered — refusing to publish an empty "
            "scan, which would read as a fleet with zero ungated repos.",
            file=sys.stderr,
        )
        return 1

    print(f"Scanning {len(repos)} repos for {args.required_context!r}", file=sys.stderr)

    ordered = sorted(repos)
    records: list = []
    for index, repo in enumerate(ordered, start=1):
        record = scan_repo(repo, args.required_context, args.pr_sample)
        records.append(record)
        # Per-repo progress, not just a final tally: a fleet sweep is minutes
        # long, and without this a stall on one slow repo is indistinguishable
        # from a hung job.
        print(f"[{index}/{len(ordered)}] {repo}: {record['status']}", file=sys.stderr)

    fleet = build_fleet(records, args.required_context)
    write_outputs(records, fleet, args.out)

    print(
        f"Fleet: {fleet['gated']}/{fleet['fleetSize']} gated, "
        f"{fleet['notGated']} not gated, {fleet['unknown']} unknown; "
        f"a ruleset requires a pull request on "
        f"{fleet['rulesetRequiresPullRequest']}/{fleet['fleetSize']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
