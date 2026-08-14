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
2. Is it **bypassable** — bypass actors on the ruleset, or a default branch that
   does not require a pull request at all (direct push skips the check)?
3. Is the check **actually arriving** on pull requests, or configured-but-never-
   reporting? That third state blocks every PR with nothing red to point at, and
   the pressure it creates is to *remove* the requirement — so it has to be
   distinguishable from healthy enforcement rather than lumped in with it.

Enumerating the fleet here (rather than having each repo self-report) is the
whole point: a repo that has lost enforcement is *present in the output* with
``gated: false``, instead of quietly dropping out of a dashboard built from
whoever happened to publish. Absence of a repo means discovery failed, and that
is itself reported.

Reads, never writes. ``/repos/{repo}/rulesets`` is not administration-gated —
``detect_merge_queue.py`` has relied on exactly that in every consumer's CI — so
the ruleset path needs no new privilege. Classic branch protection *is* admin-
gated; a 403 there is recorded as ``unknown`` rather than silently folded into
"not protected", because those two mean opposite things.

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

# GitHub's `bypass_actors[].actor_type` for a GitHub App installation. Called
# out separately because "no bot can bypass the gate" is an explicit assertion
# of this milestone, and an App bypass is invisible among role-based entries.
_INTEGRATION_ACTOR = "Integration"

SCHEMA_VERSION = "1.0"

# Per-repo verdict. Four values, not a boolean, because "we could not read it"
# must never collapse into "it is not gated" (that would make an outage look
# like a regression) nor into "it is gated" (a false green).
STATUS_UNBYPASSABLE = "gated-unbypassable"
STATUS_BYPASSABLE = "gated-bypassable"
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
FINDING_BYPASSABLE = "gate-bypassable"
FINDING_BOT_BYPASS = "gate-bot-bypass"
FINDING_DIRECT_PUSH = "gate-direct-push-permitted"
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

# Classic-branch-protection states that mean "we could not see it", as opposed
# to `absent` (a genuine 404) or `present`.
_CLASSIC_UNREADABLE = frozenset({"forbidden", "unknown"})


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


def bypass_actors(ruleset: dict) -> list:
    """Normalise a ruleset's ``bypass_actors`` to the wire shape.

    ``bypass_mode`` matters as much as the actor: ``always`` is a standing
    exemption, ``pull_request`` only lets the actor skip the rule via a PR that
    itself still has to satisfy everything else. Both are carried through rather
    than flattened to a boolean.
    """
    out: list = []
    for actor in ruleset.get("bypass_actors") or []:
        if not isinstance(actor, dict):
            continue
        out.append(
            {
                "rulesetId": ruleset.get("id"),
                "actorType": actor.get("actor_type"),
                "actorId": actor.get("actor_id"),
                "bypassMode": actor.get("bypass_mode"),
            }
        )
    return out


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
    classic_protection: str,
    arrival_samples: Optional[list],
    has_tests_workflow_file: Optional[bool],
    required_context: str,
    errors: list,
) -> dict:
    """Build one repo's record from already-fetched payloads. No I/O.

    ``rulesets`` are *expanded* ruleset objects (the list endpoint omits rules,
    so each must be fetched individually before it reaches here).
    ``classic_protection`` is one of ``absent`` / ``present`` / ``forbidden``.
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
            "unbypassable": None,
            "status": STATUS_UNKNOWN,
            "enforcement": None,
            "bypass": None,
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
    actors: list = []
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
            # Only the rulesets that carry the gate contribute bypass actors.
            # A bypass on some unrelated ruleset does not exempt anyone from
            # this check, and counting it would overstate bypassability.
            actors.extend(bypass_actors(ruleset))
            strict = strict or strict_policy(ruleset)
        all_contexts.extend(contexts)
        requires_pr = requires_pr or has_rule(ruleset, _PULL_REQUEST_RULE)

    gated = bool(matched_rulesets)
    bot_actors = [a for a in actors if a.get("actorType") == _INTEGRATION_ACTOR]
    # Direct push is the bypass nobody configures: with no ruleset requiring a
    # pull request, anyone with write access lands on the default branch without
    # a PR for the check to attach to.
    direct_push = not requires_pr
    # An unreadable classic-protection state cannot rule out an admin
    # enforcement exemption, so it cannot support the strongest claim. It
    # downgrades `gated-unbypassable` to `gated-bypassable`, never to
    # `not-gated` — the ruleset evidence for "required" was read successfully.
    classic_unreadable = classic_protection in _CLASSIC_UNREADABLE
    bypassable = bool(actors) or direct_push or classic_unreadable

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
        if actors:
            findings.append(
                _finding(
                    FINDING_BYPASSABLE,
                    "warning",
                    f"{len(actors)} bypass actor(s) can skip the ruleset carrying "
                    f"{required_context!r}",
                )
            )
        if bot_actors:
            findings.append(
                _finding(
                    FINDING_BOT_BYPASS,
                    "error",
                    f"{len(bot_actors)} GitHub App bypass actor(s) on the ruleset "
                    "carrying the gate — automation can merge without it",
                )
            )
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
    if direct_push:
        findings.append(
            _finding(
                FINDING_DIRECT_PUSH,
                "error" if gated else "warning",
                f"no active ruleset requires a pull request on {default_branch}, so a "
                "direct push bypasses every required check",
            )
        )
    if classic_unreadable:
        findings.append(
            _finding(
                FINDING_UNREADABLE,
                "warning",
                "classic branch protection could not be read (it is admin-gated) — "
                "reported as unreadable rather than assumed absent, so this repo "
                "cannot be certified unbypassable",
            )
        )

    if not gated:
        status = STATUS_NOT_GATED
    elif bypassable:
        status = STATUS_BYPASSABLE
    else:
        status = STATUS_UNBYPASSABLE

    return {
        "schemaVersion": SCHEMA_VERSION,
        "repo": repo,
        "collectedAt": collected_at,
        "defaultBranch": default_branch,
        "requiredContext": required_context,
        "gated": gated,
        "unbypassable": gated and not bypassable,
        "status": status,
        "enforcement": {
            "required": gated,
            "source": "ruleset" if gated else "none",
            "rulesets": matched_rulesets,
            "requiredContexts": sorted(set(all_contexts)),
            "requiresPullRequest": requires_pr,
            "strictRequiredStatusChecksPolicy": strict,
            "classicProtection": classic_protection,
        },
        "bypass": {
            "bypassable": bypassable,
            "actors": actors,
            "botActors": bot_actors,
            "directPushPermitted": direct_push,
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

    gated = by_status.get(STATUS_UNBYPASSABLE, 0) + by_status.get(STATUS_BYPASSABLE, 0)
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
        "gatedUnbypassable": by_status.get(STATUS_UNBYPASSABLE, 0),
        "gatedBypassable": by_status.get(STATUS_BYPASSABLE, 0),
        "notGated": by_status.get(STATUS_NOT_GATED, 0),
        "unknown": unknown,
        "gatedPct": round(100.0 * gated / known, 1) if known else 0.0,
        "withTestsWorkflowFile": sum(
            1 for r in records if r.get("hasTestsWorkflowFile")
        ),
        "byStatus": by_status,
        "byArrival": by_arrival,
        "byFinding": by_finding,
        # Inlined so the binary count and its per-repo breakdown are one fetch.
        "repos": [
            {
                "repo": r["repo"],
                "status": r["status"],
                "gated": r["gated"],
                "unbypassable": r["unbypassable"],
            }
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


def fetch_classic_protection(repo: str, branch: str, run: RunFn = _run_gh) -> str:
    """``absent`` | ``present`` | ``forbidden``.

    A 404 genuinely means unprotected. A 403 means the token cannot see it, and
    is kept distinct — collapsing the two would let an admin-gated read report
    as "no protection here", which is a false negative in the safe-looking
    direction.
    """
    try:
        run(["api", f"repos/{repo}/branches/{branch}/protection", "--jq", ".url"])
    except GhError as exc:
        if exc.status == 404:
            return "absent"
        if exc.status == 403:
            return "forbidden"
        raise
    return "present"


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
            # CheckRun exposes `name`, StatusContext exposes `context`.
            name = ctx.get("name") or ctx.get("context")
            if name is not None:
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

    Each fetch is guarded separately so one unreadable facet (e.g. admin-gated
    classic protection) degrades that field alone instead of discarding the
    ruleset evidence that was read successfully.
    """
    errors: list = []
    default_branch = "main"
    rulesets: list = []
    classic = "absent"
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
            classic = fetch_classic_protection(repo, default_branch, run=run)
        except GhError as exc:
            # Non-fatal: the ruleset read already carries the load-bearing
            # answer, and no fleet repo has been observed on classic protection.
            classic = "unknown"
            print(f"::warning::{repo}: {exc}", file=sys.stderr)
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
        classic_protection=classic,
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
            "unbypassable": record["unbypassable"],
            "arrival": (record.get("arrival") or {}).get("status"),
        }
        with (out_dir / f"history_{slug}.jsonl").open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    (out_dir / "fleet.json").write_text(json.dumps(fleet, indent=2))
    fleet_entry = {
        "date": fleet["collectedAt"][:10],
        "fleetSize": fleet["fleetSize"],
        "gated": fleet["gated"],
        "gatedUnbypassable": fleet["gatedUnbypassable"],
        "notGated": fleet["notGated"],
        "unknown": fleet["unknown"],
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
        f"Fleet: {fleet['gated']}/{fleet['fleetSize']} gated "
        f"({fleet['gatedUnbypassable']} unbypassable, "
        f"{fleet['gatedBypassable']} bypassable), "
        f"{fleet['notGated']} not gated, {fleet['unknown']} unknown",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
