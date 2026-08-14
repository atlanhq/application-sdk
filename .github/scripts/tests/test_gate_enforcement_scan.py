"""Tests for .github/scripts/gate_enforcement_scan.py.

`gh` is stubbed through the module's single `run` seam (per the testability-seam
convention in docs/standards/ci.md), so every branch — gated, ungated,
bypassable, unreadable, and each arrival verdict — is exercised without network
access.

The load-bearing assertion in here is the *fail-loud* one: an unreadable repo
must never produce the same record as a readable-but-ungated repo, and must
never produce the same record as a gated one. A scanner whose whole purpose is
to stop a false green cannot itself manufacture one out of an auth error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gate_enforcement_scan import (  # noqa: E402
    ARRIVAL_INTERMITTENT,
    ARRIVAL_NEVER,
    ARRIVAL_NO_DATA,
    ARRIVAL_REPORTING,
    ARRIVAL_UNKNOWN,
    DEFAULT_NAME_PATTERN,
    DEFAULT_REQUIRED_CONTEXT,
    FINDING_BOT_BYPASS,
    FINDING_BYPASSABLE,
    FINDING_DIRECT_PUSH,
    FINDING_NOT_ARRIVING,
    FINDING_NOT_REQUIRED,
    FINDING_UNPRODUCIBLE,
    FINDING_UNREADABLE,
    STATUS_BYPASSABLE,
    STATUS_NOT_GATED,
    STATUS_UNBYPASSABLE,
    STATUS_UNKNOWN,
    GhError,
    build_fleet,
    bypass_actors,
    classify_arrival,
    evaluate_repo,
    fetch_classic_protection,
    list_fleet_repos,
    parse_arrival_nodes,
    required_contexts,
    scan_repo,
    write_outputs,
)

REPO = "atlanhq/atlan-example-app"
GATE = DEFAULT_REQUIRED_CONTEXT


def _ruleset(
    *,
    ruleset_id=1,
    contexts=(GATE,),
    include=("~DEFAULT_BRANCH",),
    exclude=(),
    enforcement="active",
    target="branch",
    with_pull_request=True,
    bypass=(),
    strict=False,
) -> dict:
    """A ruleset payload shaped like the real /repos/{repo}/rulesets/{id} body."""
    rules = []
    if with_pull_request:
        rules.append({"type": "pull_request", "parameters": {}})
    if contexts is not None:
        rules.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": strict,
                    "required_status_checks": [
                        {"context": c, "integration_id": 15368} for c in contexts
                    ],
                },
            }
        )
    return {
        "id": ruleset_id,
        "name": "main",
        "source_type": "Repository",
        "target": target,
        "enforcement": enforcement,
        "conditions": {
            "ref_name": {"include": list(include), "exclude": list(exclude)}
        },
        "rules": rules,
        "bypass_actors": list(bypass),
    }


def _evaluate(**overrides) -> dict:
    kwargs = {
        "repo": REPO,
        "default_branch": "main",
        "rulesets": [_ruleset()],
        "classic_protection": "absent",
        "arrival_samples": [{"found": True, "truncated": False}],
        "has_tests_workflow_file": True,
        "required_context": GATE,
        "errors": [],
    }
    kwargs.update(overrides)
    return evaluate_repo(**kwargs)


def _finding_ids(record: dict) -> set:
    return {f["id"] for f in record["findings"]}


# --- the real payload shape ------------------------------------------------


def test_gate_context_matches_a_real_ruleset_payload():
    """Pin the context spelling against a verbatim slice of a live ruleset.

    GitHub composes the context from the *caller job id* plus the reusable
    workflow's job name, and omits the workflow name entirely. Guessing
    `Tests / Tests Gate` (title-cased, as the milestone prose writes it) would
    match nothing and report the whole fleet as ungated — a silent, total false
    negative. This is the assertion that stops that.
    """
    live_slice = {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [
                {"context": "scan / Build Image", "integration_id": 15368},
                {"context": "pre-commit", "integration_id": 15368},
                {"context": "suite / Conformance Gate", "integration_id": 15368},
                {"context": "tests / Tests Gate", "integration_id": 15368},
            ],
        },
    }
    contexts = required_contexts({"rules": [live_slice]})
    assert DEFAULT_REQUIRED_CONTEXT in contexts


def test_required_contexts_ignores_other_rule_types():
    ruleset = _ruleset(contexts=("a", "b"))
    assert required_contexts(ruleset) == ["a", "b"]


# --- enforcement -----------------------------------------------------------


def test_gated_unbypassable_is_the_only_clean_state():
    record = _evaluate()
    assert record["gated"] is True
    assert record["unbypassable"] is True
    assert record["status"] == STATUS_UNBYPASSABLE
    assert record["findings"] == []


def test_context_required_on_another_branch_does_not_count():
    """A ruleset scoped to a release line says nothing about the default branch."""
    record = _evaluate(rulesets=[_ruleset(include=("release/*",))])
    assert record["status"] == STATUS_NOT_GATED
    assert FINDING_NOT_REQUIRED in _finding_ids(record)


@pytest.mark.parametrize("enforcement", ["evaluate", "disabled"])
def test_non_active_enforcement_is_not_gated(enforcement):
    """`evaluate` is GitHub's dry-run mode: it reports and never blocks."""
    record = _evaluate(rulesets=[_ruleset(enforcement=enforcement)])
    assert record["status"] == STATUS_NOT_GATED


def test_excluded_branch_wins_over_include():
    record = _evaluate(rulesets=[_ruleset(include=("~ALL",), exclude=("main",))])
    assert record["status"] == STATUS_NOT_GATED


def test_other_required_checks_are_reported_so_a_rename_is_visible():
    """A renamed gate must read as 'not required, but these are' — not silence."""
    record = _evaluate(rulesets=[_ruleset(contexts=("tests / Tests Gateway",))])
    assert record["status"] == STATUS_NOT_GATED
    assert record["enforcement"]["requiredContexts"] == ["tests / Tests Gateway"]
    assert "tests / Tests Gateway" in record["findings"][0]["message"]


def test_no_rulesets_at_all():
    record = _evaluate(rulesets=[])
    assert record["status"] == STATUS_NOT_GATED
    assert record["enforcement"]["requiredContexts"] == []
    assert FINDING_DIRECT_PUSH in _finding_ids(record)


# --- bypass ----------------------------------------------------------------


def test_bypass_actor_downgrades_to_bypassable():
    record = _evaluate(
        rulesets=[
            _ruleset(
                bypass=[
                    {
                        "actor_id": 5,
                        "actor_type": "RepositoryRole",
                        "bypass_mode": "always",
                    }
                ]
            )
        ]
    )
    assert record["gated"] is True
    assert record["unbypassable"] is False
    assert record["status"] == STATUS_BYPASSABLE
    assert _finding_ids(record) == {FINDING_BYPASSABLE}
    assert record["bypass"]["actors"][0]["bypassMode"] == "always"


def test_app_bypass_is_called_out_separately():
    """'No bot can bypass the gate' is an explicit assertion of this milestone,
    and an App entry is invisible among role-based ones without its own finding."""
    record = _evaluate(
        rulesets=[
            _ruleset(
                bypass=[
                    {
                        "actor_id": 62283865,
                        "actor_type": "Integration",
                        "bypass_mode": "always",
                    }
                ]
            )
        ]
    )
    assert FINDING_BOT_BYPASS in _finding_ids(record)
    assert len(record["bypass"]["botActors"]) == 1


def test_bypass_on_an_unrelated_ruleset_does_not_count():
    """A bypass only exempts the ruleset it sits on. Attributing an unrelated
    ruleset's bypass to the gate would overstate bypassability."""
    unrelated = _ruleset(
        ruleset_id=2,
        contexts=("pre-commit",),
        bypass=[
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        ],
    )
    record = _evaluate(rulesets=[_ruleset(), unrelated])
    assert record["status"] == STATUS_UNBYPASSABLE
    assert record["bypass"]["actors"] == []


def test_no_pull_request_requirement_means_direct_push_bypasses_everything():
    record = _evaluate(rulesets=[_ruleset(with_pull_request=False)])
    assert record["bypass"]["directPushPermitted"] is True
    assert record["status"] == STATUS_BYPASSABLE
    assert FINDING_DIRECT_PUSH in _finding_ids(record)


def test_pull_request_rule_may_live_on_a_second_ruleset():
    """PR-required and checks-required are commonly split across rulesets;
    requiring both on one object would report a false direct-push hole."""
    checks_only = _ruleset(ruleset_id=1, with_pull_request=False)
    pr_only = _ruleset(ruleset_id=2, contexts=None, with_pull_request=True)
    record = _evaluate(rulesets=[checks_only, pr_only])
    assert record["bypass"]["directPushPermitted"] is False
    assert record["status"] == STATUS_UNBYPASSABLE


@pytest.mark.parametrize("classic", ["forbidden", "unknown"])
def test_unreadable_classic_protection_blocks_the_unbypassable_claim(classic):
    """Downgrades the claim, never the fact: the repo stays `gated`."""
    record = _evaluate(classic_protection=classic)
    assert record["gated"] is True
    assert record["status"] == STATUS_BYPASSABLE
    assert FINDING_UNREADABLE in _finding_ids(record)


def test_bypass_actors_normalisation_carries_the_mode():
    actors = bypass_actors(
        _ruleset(
            ruleset_id=9,
            bypass=[
                {
                    "actor_id": 1,
                    "actor_type": "OrganizationAdmin",
                    "bypass_mode": "pull_request",
                }
            ],
        )
    )
    assert actors == [
        {
            "rulesetId": 9,
            "actorType": "OrganizationAdmin",
            "actorId": 1,
            "bypassMode": "pull_request",
        }
    ]


# --- arrival ---------------------------------------------------------------


@pytest.mark.parametrize(
    "samples,expected",
    [
        ([{"found": True, "truncated": False}] * 3, ARRIVAL_REPORTING),
        ([{"found": False, "truncated": False}] * 3, ARRIVAL_NEVER),
        (
            [{"found": True, "truncated": False}, {"found": False, "truncated": False}],
            ARRIVAL_INTERMITTENT,
        ),
        ([], ARRIVAL_NO_DATA),
        ([{"found": False, "truncated": True}], ARRIVAL_UNKNOWN),
    ],
)
def test_classify_arrival(samples, expected):
    assert classify_arrival(samples)[0] == expected


def test_truncated_samples_leave_the_denominator():
    """>100 contexts on a commit with the gate not among the first 100 proves
    nothing; counting it as a miss would invent a never-arriving gate."""
    verdict, sampled, found, truncated = classify_arrival(
        [{"found": True, "truncated": False}, {"found": False, "truncated": True}]
    )
    assert (verdict, sampled, found, truncated) == (ARRIVAL_REPORTING, 1, 1, 1)


def test_a_truncated_sample_that_found_the_gate_still_counts():
    verdict, sampled, found, _ = classify_arrival([{"found": True, "truncated": True}])
    assert (verdict, sampled, found) == (ARRIVAL_REPORTING, 1, 1)


def test_required_but_never_arriving_is_a_finding():
    record = _evaluate(arrival_samples=[{"found": False, "truncated": False}] * 5)
    assert record["status"] == STATUS_UNBYPASSABLE
    assert FINDING_NOT_ARRIVING in _finding_ids(record)
    assert record["arrival"]["prsWithContext"] == 0


def test_never_arriving_is_not_reported_when_the_gate_is_not_required():
    """An ungated repo with no gate runs is ordinary, not a stall."""
    record = _evaluate(
        rulesets=[],
        arrival_samples=[{"found": False, "truncated": False}] * 5,
    )
    assert FINDING_NOT_ARRIVING not in _finding_ids(record)


def test_gated_never_arriving_without_a_workflow_file_is_the_deadlock_case():
    record = _evaluate(
        has_tests_workflow_file=False,
        arrival_samples=[{"found": False, "truncated": False}] * 5,
    )
    assert FINDING_UNPRODUCIBLE in _finding_ids(record)


def test_a_missing_workflow_file_alone_is_not_a_finding():
    """Observed on the real fleet: repos with no `.github/workflows/tests.yaml`
    still emit `tests / Tests Gate`, because the context is composed from the
    caller *job id*, not the file name. Treating file absence as proof would
    invent a deadlock on a repo whose gate demonstrably reports."""
    record = _evaluate(
        has_tests_workflow_file=False,
        arrival_samples=[{"found": True, "truncated": False}] * 4,
    )
    assert FINDING_UNPRODUCIBLE not in _finding_ids(record)
    assert record["findings"] == []


def test_parse_arrival_nodes_reads_both_context_shapes():
    """CheckRun exposes `name`, StatusContext exposes `context`; reading only
    one silently halves the evidence."""
    payload = {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "number": 1,
                            "commits": {
                                "nodes": [
                                    {
                                        "commit": {
                                            "statusCheckRollup": {
                                                "contexts": {
                                                    "totalCount": 2,
                                                    "nodes": [
                                                        {
                                                            "__typename": "CheckRun",
                                                            "name": GATE,
                                                        },
                                                        {
                                                            "__typename": "StatusContext",
                                                            "context": "pre-commit",
                                                        },
                                                    ],
                                                }
                                            }
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
    }
    assert parse_arrival_nodes(payload, GATE) == [
        {"number": 1, "found": True, "truncated": False}
    ]


def test_parse_arrival_nodes_flags_truncation():
    payload = {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "number": 2,
                            "commits": {
                                "nodes": [
                                    {
                                        "commit": {
                                            "statusCheckRollup": {
                                                "contexts": {
                                                    "totalCount": 140,
                                                    "nodes": [
                                                        {
                                                            "__typename": "CheckRun",
                                                            "name": "x",
                                                        }
                                                    ],
                                                }
                                            }
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
    }
    assert parse_arrival_nodes(payload, GATE)[0]["truncated"] is True


def test_parse_arrival_nodes_skips_commits_with_no_checks():
    """No rollup at all carries no information either way."""
    payload = {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "number": 3,
                            "commits": {
                                "nodes": [{"commit": {"statusCheckRollup": None}}]
                            },
                        }
                    ]
                }
            }
        }
    }
    assert parse_arrival_nodes(payload, GATE) == []


# --- fail-loud -------------------------------------------------------------


def test_unreadable_repo_is_unknown_not_ungated():
    """The assertion this whole module exists for."""
    record = _evaluate(errors=["gh api failed: HTTP 401"])
    assert record["status"] == STATUS_UNKNOWN
    assert record["gated"] is None
    assert record["unbypassable"] is None
    assert _finding_ids(record) == {FINDING_UNREADABLE}
    assert "HTTP 401" in record["findings"][0]["message"]


def test_scan_repo_reports_unknown_when_rulesets_are_unreadable():
    def run(args: list) -> str:
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        raise GhError("gh api failed: HTTP 403", status=403)

    record = scan_repo(REPO, GATE, sample_size=0, run=run)
    assert record["status"] == STATUS_UNKNOWN


def test_scan_repo_survives_a_failed_arrival_probe():
    """A GraphQL outage must not discard the ruleset evidence already read."""

    def run(args: list) -> str:
        if args[1] == "graphql":
            raise GhError("gh api failed: HTTP 502", status=502)
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}]])
        if args[1] == f"repos/{REPO}/rulesets/1":
            return json.dumps(_ruleset())
        return json.dumps("ok")

    record = scan_repo(REPO, GATE, sample_size=5, run=run)
    assert record["status"] == STATUS_UNBYPASSABLE
    assert record["arrival"]["status"] == ARRIVAL_NO_DATA


def test_scan_repo_reads_a_gated_repo_end_to_end():
    calls: list = []

    def run(args: list) -> str:
        calls.append(args[1])
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}]])
        if args[1] == f"repos/{REPO}/rulesets/1":
            return json.dumps(_ruleset())
        if args[1].endswith("/protection"):
            raise GhError("gh api failed: HTTP 404", status=404)
        return json.dumps("sha")

    record = scan_repo(REPO, GATE, sample_size=0, run=run)
    assert record["status"] == STATUS_UNBYPASSABLE
    assert record["enforcement"]["classicProtection"] == "absent"
    assert record["hasTestsWorkflowFile"] is True
    assert f"repos/{REPO}/rulesets?includes_parents=true" in calls


@pytest.mark.parametrize("status,expected", [(404, "absent"), (403, "forbidden")])
def test_classic_protection_distinguishes_404_from_403(status, expected):
    """404 means unprotected; 403 means we cannot see it. Collapsing them would
    let an admin-gated read report as 'no protection here'."""

    def run(args: list) -> str:
        raise GhError("boom", status=status)

    assert fetch_classic_protection(REPO, "main", run=run) == expected


def test_classic_protection_reraises_unexpected_failures():
    def run(args: list) -> str:
        raise GhError("boom", status=500)

    with pytest.raises(GhError):
        fetch_classic_protection(REPO, "main", run=run)


# --- discovery + rollup ----------------------------------------------------


def test_list_fleet_repos_filters_by_name_pattern():
    def run(args: list) -> str:
        return json.dumps(
            [
                "atlanhq/atlan-mysql-app",
                "atlanhq/application-sdk",
                "atlanhq/connectors-sql",
                "atlanhq/atlan-openapi-app",
            ]
        )

    assert list_fleet_repos("atlanhq", DEFAULT_NAME_PATTERN, run=run) == [
        "atlanhq/atlan-mysql-app",
        "atlanhq/atlan-openapi-app",
    ]


def test_build_fleet_counts_the_headline_binary():
    records = [
        _evaluate(repo="atlanhq/a"),
        _evaluate(
            repo="atlanhq/b",
            rulesets=[
                _ruleset(
                    bypass=[
                        {
                            "actor_id": 5,
                            "actor_type": "RepositoryRole",
                            "bypass_mode": "always",
                        }
                    ]
                )
            ],
        ),
        _evaluate(repo="atlanhq/c", rulesets=[]),
        _evaluate(repo="atlanhq/d", errors=["HTTP 500"]),
    ]
    fleet = build_fleet(records, GATE)
    assert fleet["fleetSize"] == 4
    assert fleet["gated"] == 2
    assert fleet["gatedUnbypassable"] == 1
    assert fleet["gatedBypassable"] == 1
    assert fleet["notGated"] == 1
    assert fleet["unknown"] == 1
    assert len(fleet["repos"]) == 4


def test_unknown_repos_are_excluded_from_the_percentage():
    """An auth outage must not read as the fleet regressing."""
    records = [_evaluate(repo="atlanhq/a")] + [
        _evaluate(repo=f"atlanhq/{n}", errors=["HTTP 500"]) for n in "bcd"
    ]
    fleet = build_fleet(records, GATE)
    assert fleet["gatedPct"] == 100.0
    assert fleet["unknown"] == 3


def test_write_outputs_layout(tmp_path):
    records = [_evaluate(repo="atlanhq/atlan-mysql-app")]
    fleet = build_fleet(records, GATE)
    write_outputs(records, fleet, tmp_path)

    repo_doc = json.loads(
        (tmp_path / "repos" / "atlanhq_atlan-mysql-app.json").read_text()
    )
    assert repo_doc["repo"] == "atlanhq/atlan-mysql-app"
    assert json.loads((tmp_path / "fleet.json").read_text())["gated"] == 1

    history = (tmp_path / "history_atlanhq_atlan-mysql-app.jsonl").read_text().strip()
    assert json.loads(history)["status"] == STATUS_UNBYPASSABLE
    assert (
        json.loads((tmp_path / "history_fleet.jsonl").read_text().strip())["gated"] == 1
    )


def test_history_is_append_only(tmp_path):
    records = [_evaluate(repo="atlanhq/atlan-mysql-app")]
    fleet = build_fleet(records, GATE)
    write_outputs(records, fleet, tmp_path)
    write_outputs(records, fleet, tmp_path)
    lines = (tmp_path / "history_fleet.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
