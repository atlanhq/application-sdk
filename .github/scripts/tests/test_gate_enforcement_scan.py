"""Tests for .github/scripts/gate_enforcement_scan.py.

`gh` is stubbed through the module's single `run` seam (per the testability-seam
convention in docs/standards/ci.md), so every branch — gated, ungated,
unreadable, and each arrival verdict — is exercised without network access.

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
    FINDING_NOT_ARRIVING,
    FINDING_NOT_REQUIRED,
    FINDING_UNPRODUCIBLE,
    FINDING_UNREADABLE,
    STATUS_GATED,
    STATUS_NOT_GATED,
    STATUS_UNKNOWN,
    GhError,
    build_fleet,
    classify_arrival,
    evaluate_repo,
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


def test_gated_with_no_findings_is_the_clean_state():
    record = _evaluate()
    assert record["gated"] is True
    assert record["status"] == STATUS_GATED
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
    assert record["enforcement"]["rulesetRequiresPullRequest"] is False


# --- bypass is not claimed --------------------------------------------------


def test_bypass_actors_are_never_reported_even_when_present():
    """The regression guard for the false green that shipped once.

    GitHub *omits* `bypass_actors` entirely — not an empty list, not a 403 — for
    any caller without admin on the repo, while still returning `rules`. Under a
    fleet-scoped token "has standing admin bypass" and "has none" are therefore
    the same bytes. The first CI run reported all five gated repos unbypassable;
    four of them had bypass actors an admin token could see.

    So the claim is not made at all. This asserts the payload carries no
    bypassability verdict even when the actors ARE visible — because a field
    that is only correct under one token is worse than no field.
    """
    record = _evaluate(
        rulesets=[
            _ruleset(
                bypass=[
                    {
                        "actor_id": 5,
                        "actor_type": "RepositoryRole",
                        "bypass_mode": "always",
                    },
                    {
                        "actor_id": 62283865,
                        "actor_type": "Integration",
                        "bypass_mode": "always",
                    },
                ]
            )
        ]
    )
    assert record["status"] == STATUS_GATED
    assert "bypass" not in record
    assert "unbypassable" not in record
    assert not any("bypass" in key.lower() for key in record["enforcement"])
    assert record["findings"] == []


def test_the_record_reads_identically_with_and_without_visible_bypass_actors():
    """Same repo, admin token vs fleet token. If these ever diverge, the scanner
    has started making a claim whose truth depends on who asked."""
    admin_view = _evaluate(
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
    # The fleet token's view: the key is absent, not empty.
    fleet_ruleset = _ruleset()
    del fleet_ruleset["bypass_actors"]
    fleet_view = _evaluate(rulesets=[fleet_ruleset])

    for view in (admin_view, fleet_view):
        view.pop("collectedAt")
    assert admin_view == fleet_view


def test_absent_pull_request_rule_is_reported_as_a_ruleset_fact_only():
    """What `rules` shows, and nothing beyond it.

    A missing `pull_request` rule means no *ruleset* requires a PR here. It does
    NOT mean a direct push is permitted — classic branch protection is the other
    enforcement mechanism, it is admin-gated, and this token cannot read it. So
    the field is named for its scope and no finding is raised: an unprovable
    claim is left unmade.
    """
    record = _evaluate(rulesets=[_ruleset(with_pull_request=False)])
    assert record["enforcement"]["rulesetRequiresPullRequest"] is False
    assert record["status"] == STATUS_GATED  # still required — a separate fact
    assert record["findings"] == []


def test_the_retracted_direct_push_claim_is_not_reported_anywhere():
    """Regression guard for the second false green (schema 3.0).

    `directPushPermitted` was `not requires_pr`, published as a fleet finding on
    69 of 77 repos — which was exactly the set whose classic branch protection
    the token could not read. GitHub returns 404 for both "no classic protection"
    (`"Branch not protected"`) and "you may not look" (`"Not Found"`), so the
    field could only ever restate the token's blind spot.

    Same shape of mistake as `bypass_actors`, so it gets the same guard: assert
    the payload makes no direct-push claim at all, under the exact input that
    used to produce one.
    """
    record = _evaluate(rulesets=[_ruleset(with_pull_request=False)])
    assert "directPushPermitted" not in record["enforcement"]
    assert not any("directpush" in key.lower() for key in record["enforcement"])
    assert not any(
        "direct-push" in finding["id"] or "direct push" in finding["message"]
        for finding in record["findings"]
    )


def test_pull_request_rule_may_live_on_a_second_ruleset():
    """PR-required and checks-required are commonly split across rulesets;
    requiring both on one object would understate PR coverage."""
    checks_only = _ruleset(ruleset_id=1, with_pull_request=False)
    pr_only = _ruleset(ruleset_id=2, contexts=None, with_pull_request=True)
    record = _evaluate(rulesets=[checks_only, pr_only])
    assert record["enforcement"]["rulesetRequiresPullRequest"] is True
    assert record["findings"] == []


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
    assert record["status"] == STATUS_GATED
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


def test_parse_arrival_nodes_raises_on_a_structurally_malformed_body():
    """`{"data": {}}` or `repository: null` is GraphQL schema/response drift,
    not "no PRs had the context" — coercing it to zero samples would read as a
    clean `no-data` instead of `unknown`."""
    with pytest.raises(GhError, match="malformed arrival payload"):
        parse_arrival_nodes({"data": {}}, GATE)
    with pytest.raises(GhError, match="malformed arrival payload"):
        parse_arrival_nodes({"data": {"repository": None}}, GATE)
    with pytest.raises(GhError, match="malformed arrival payload"):
        # a truthy non-dict pullRequests must raise GhError, not AttributeError
        parse_arrival_nodes(
            {"data": {"repository": {"pullRequests": "unexpected"}}}, GATE
        )


def _arrival_payload(pr_node) -> dict:
    return {"data": {"repository": {"pullRequests": {"nodes": [pr_node]}}}}


@pytest.mark.parametrize(
    "pr_node",
    [
        pytest.param({"number": 1, "commits": "unexpected"}, id="commits"),
        pytest.param(
            {"number": 1, "commits": {"nodes": ["unexpected"]}}, id="commits.nodes[0]"
        ),
        pytest.param(
            {"number": 1, "commits": {"nodes": [{"commit": "unexpected"}]}},
            id="commit",
        ),
        pytest.param(
            {
                "number": 1,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": "unexpected"}}]},
            },
            id="statusCheckRollup",
        ),
        pytest.param(
            {
                "number": 1,
                "commits": {
                    "nodes": [
                        {"commit": {"statusCheckRollup": {"contexts": "unexpected"}}}
                    ]
                },
            },
            id="contexts",
        ),
        pytest.param("unexpected", id="pullRequests.nodes[0]"),
    ],
)
def test_a_truthy_non_dict_anywhere_in_the_walk_raises_gh_error(pr_node):
    """Every nested level, not just the spine.

    `(value or {}).get(...)` raises AttributeError on a truthy non-dict, and
    `scan_repo` catches only GhError — so one malformed body would abort the
    whole fleet sweep instead of marking that one repo `unknown`. That is the
    same false-green-by-abort the top-level guard prevents, one level down.
    """
    with pytest.raises(GhError, match="malformed arrival payload"):
        parse_arrival_nodes(_arrival_payload(pr_node), GATE)


def test_a_non_integer_total_count_raises_rather_than_comparing():
    """`total > len(...)` against a string is a TypeError, uncaught, mid-sweep."""
    payload = _arrival_payload(
        {
            "number": 1,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {"totalCount": "many", "nodes": []}
                            }
                        }
                    }
                ]
            },
        }
    )
    with pytest.raises(GhError, match="totalCount"):
        parse_arrival_nodes(payload, GATE)


def test_nulls_along_the_walk_are_a_legitimate_skip_not_an_error():
    """GraphQL returns every requested field, so a null is the schema's own
    "nothing here" — distinct from a wrong-typed value, which is drift."""
    for pr_node in (
        None,
        {"number": 1, "commits": None},
        {"number": 1, "commits": {"nodes": None}},
        {"number": 1, "commits": {"nodes": [{"commit": None}]}},
    ):
        assert parse_arrival_nodes(_arrival_payload(pr_node), GATE) == []


@pytest.mark.parametrize(
    "ctx_node",
    [
        pytest.param(
            {"__typename": "CheckRun", "name": ["unexpected"]}, id="name-list"
        ),
        pytest.param({"__typename": "CheckRun", "name": {"x": 1}}, id="name-dict"),
        pytest.param({"__typename": "CheckRun", "name": 5}, id="name-int"),
        pytest.param({"__typename": "CheckRun", "name": True}, id="name-bool"),
        # Falsy wrong-typed leaves: an `or`-chain would collapse these to the
        # fallback/`None` and skip them as "absent", reading as a clean miss.
        pytest.param({"__typename": "CheckRun", "name": 0}, id="name-zero"),
        pytest.param({"__typename": "CheckRun", "name": False}, id="name-false"),
        pytest.param({"__typename": "CheckRun", "name": []}, id="name-empty-list"),
        pytest.param(
            {"__typename": "StatusContext", "context": ["unexpected"]},
            id="context-list",
        ),
    ],
)
def test_a_non_string_context_leaf_raises_gh_error(ctx_node):
    """The leaf analogue of the container guards.

    An unhashable `name`/`context` (list/dict) raises an uncaught TypeError in
    `names.add(...)` — which `scan_repo` does not catch — and a hashable scalar
    (int/bool) never matches the required-context string, silently reading as
    `found: False`. Falsy wrong-typed values (`0`/`False`/`[]`) are the same
    escape one branch down: selected by truthiness they collapse to "absent"
    and are skipped. Either way a malformed body must surface as GhError so the
    one repo degrades to `unknown` instead of aborting the fleet sweep.
    """
    pr_node = {
        "number": 1,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "contexts": {"totalCount": 1, "nodes": [ctx_node]}
                        }
                    }
                }
            ]
        },
    }
    with pytest.raises(GhError, match="malformed arrival payload"):
        parse_arrival_nodes(_arrival_payload(pr_node), GATE)


def test_scan_repo_reports_unknown_when_an_arrival_leaf_is_malformed():
    """Pin the downstream effect: a wrong-typed `name`/`context` leaf reaches
    `scan_repo` as a caught GhError, not an uncaught TypeError — so the repo
    degrades to `unknown` (fail-loud) instead of aborting the fleet sweep.
    The ruleset-detail read also fails here so the whole record, not just the
    arrival facet, lands on `unknown`."""

    def run(args: list) -> str:
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}]])
        if args[1] == "graphql":
            return json.dumps(
                _arrival_payload(
                    {
                        "number": 1,
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "totalCount": 1,
                                                "nodes": [
                                                    {
                                                        "__typename": "CheckRun",
                                                        "name": ["unexpected"],
                                                    }
                                                ],
                                            }
                                        }
                                    }
                                }
                            ]
                        },
                    }
                )
            )
        raise GhError("gh api failed: HTTP 429", status=429)

    record = scan_repo(REPO, GATE, sample_size=5, run=run)
    assert record["status"] == STATUS_UNKNOWN
    assert _finding_ids(record) == {FINDING_UNREADABLE}


def test_scan_repo_marks_arrival_no_data_when_a_leaf_is_malformed():
    """The arrival facet's degradation, pinned in isolation: with healthy
    ruleset reads, a malformed context leaf surfaces as a caught GhError, so
    the arrival facet reads `no-data` — never `never-arriving` — while the
    ruleset evidence still evaluates. The facet failure must not be masked by,
    or confused with, an unrelated read error, and must not aggregate into a
    false `NOT_ARRIVING` finding."""

    def run(args: list) -> str:
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}]])
        if args[1] == f"repos/{REPO}/rulesets/1":
            return json.dumps(_ruleset())
        if args[1] == "graphql":
            return json.dumps(
                _arrival_payload(
                    {
                        "number": 1,
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "totalCount": 1,
                                                "nodes": [
                                                    {
                                                        "__typename": "CheckRun",
                                                        "name": ["unexpected"],
                                                    }
                                                ],
                                            }
                                        }
                                    }
                                }
                            ]
                        },
                    }
                )
            )
        return json.dumps("ok")

    record = scan_repo(REPO, GATE, sample_size=5, run=run)
    # The ruleset read succeeded, so the repo still evaluates clean on that
    # evidence; only the arrival facet carries the read failure.
    assert record["status"] == STATUS_GATED
    assert record["arrival"]["status"] == ARRIVAL_NO_DATA
    assert record["arrival"]["prsSampled"] == 0
    assert FINDING_NOT_ARRIVING not in _finding_ids(record)


def test_repeated_contexts_on_one_commit_are_not_truncation():
    """A commit routinely carries the same context several times — a bot PR
    stacks 5-7 gate runs on one SHA, all but the newest cancelled by the
    concurrency group. Measuring truncation against the *deduplicated* names
    marked every busy repo truncated, which silently converted real
    never-arriving evidence into `unknown`."""
    nodes = [{"__typename": "CheckRun", "name": GATE} for _ in range(3)]
    nodes += [{"__typename": "CheckRun", "name": "tests / Unit"} for _ in range(4)]
    payload = _arrival_payload(
        {
            "number": 1,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {"totalCount": len(nodes), "nodes": nodes}
                            }
                        }
                    }
                ]
            },
        }
    )
    sample = parse_arrival_nodes(payload, GATE)[0]
    assert sample["found"] is True
    assert sample["truncated"] is False  # 7 returned of 7 — nothing was cut off


def test_scan_repo_reports_unknown_when_a_ruleset_detail_fails():
    """A ruleset-detail GET that fails must not read as "no gate here" — the
    repo must go `unknown`, never be evaluated on a partially-expanded list."""

    def run(args: list) -> str:
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}, {"id": 2}]])
        if args[1] == f"repos/{REPO}/rulesets/1":
            return json.dumps(_ruleset(ruleset_id=1))
        raise GhError("gh api failed: HTTP 429", status=429)

    record = scan_repo(REPO, GATE, sample_size=0, run=run)
    assert record["status"] == STATUS_UNKNOWN
    assert record["gated"] is None
    assert _finding_ids(record) == {FINDING_UNREADABLE}


# --- fail-loud -------------------------------------------------------------


def test_unreadable_repo_is_unknown_not_ungated():
    """The assertion this whole module exists for."""
    record = _evaluate(errors=["gh api failed: HTTP 401"])
    assert record["status"] == STATUS_UNKNOWN
    assert record["gated"] is None
    assert record["enforcement"] is None
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
    assert record["status"] == STATUS_GATED
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
        return json.dumps("sha")

    record = scan_repo(REPO, GATE, sample_size=0, run=run)
    assert record["status"] == STATUS_GATED
    assert record["hasTestsWorkflowFile"] is True
    assert f"repos/{REPO}/rulesets?includes_parents=true" in calls
    # Classic branch protection is admin-gated, so it could never be read with
    # this token and is not consulted at all. Probing it would spend an API call
    # per repo to learn nothing.
    assert not any(str(c).endswith("/protection") for c in calls)


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
        _evaluate(repo="atlanhq/b", rulesets=[_ruleset(with_pull_request=False)]),
        _evaluate(repo="atlanhq/c", rulesets=[]),
        _evaluate(repo="atlanhq/d", errors=["HTTP 500"]),
    ]
    fleet = build_fleet(records, GATE)
    assert fleet["fleetSize"] == 4
    assert fleet["gated"] == 2
    # Only repo `a` has a ruleset with a `pull_request` rule: `b` was built
    # without one and `c` has no rulesets, while `d` is unreadable and carries no
    # enforcement object at all.
    assert fleet["rulesetRequiresPullRequest"] == 1
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
    assert json.loads(history)["status"] == STATUS_GATED
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
