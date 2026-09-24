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
    ARRIVAL_OVERFETCH,
    ARRIVAL_REPORTING,
    ARRIVAL_UNKNOWN,
    DEFAULT_NAME_PATTERN,
    DEFAULT_REQUIRED_CONTEXT,
    FINDING_ARRIVAL_UNREADABLE,
    FINDING_NOT_ARRIVING,
    FINDING_NOT_REQUIRED,
    FINDING_UNPRODUCIBLE,
    FINDING_UNREADABLE,
    MAX_ARRIVAL_FETCH,
    MAX_CONTEXT_PAGES,
    STATUS_GATED,
    STATUS_NOT_GATED,
    STATUS_UNKNOWN,
    GhError,
    build_fleet,
    classify_arrival,
    evaluate_repo,
    fetch_arrival_samples,
    fetch_tests_workflow_last_modified,
    list_fleet_repos,
    parse_arrival_nodes,
    parse_contexts_response,
    required_contexts,
    scan_repo,
    select_arrival_samples,
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
        {
            "number": 1,
            "found": True,
            "truncated": False,
            # Paging and staleness handles: absent from this fixture, and
            # stripped again by `fetch_arrival_samples` before the sample
            # reaches the record. One walk: an open-shaped PR with no
            # `mergeCommit` reads the head only.
            "walks": [
                {"oid": None, "cursor": None, "truncated": False, "key": "names"}
            ],
            "committedDate": None,
        }
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


# --- FND-1947: sampled population + context paging -------------------------


def _ctx(name: str) -> dict:
    return {"__typename": "CheckRun", "name": name}


def _contexts(names, *, total=None, has_next=False, cursor=None) -> dict:
    nodes = [_ctx(n) for n in names]
    return {
        "totalCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }


def _arrival_with(contexts: dict, *, number=1, oid="c0ffee") -> dict:
    return _arrival_payload(
        {
            "number": number,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": oid,
                            "statusCheckRollup": {"contexts": contexts},
                        }
                    }
                ]
            },
        }
    )


def _page(contexts: dict) -> dict:
    return {
        "data": {
            "repository": {"object": {"statusCheckRollup": {"contexts": contexts}}}
        }
    }


def _arrival_two_prs() -> str:
    """Two samples: #1 conclusive on page one, #2 truncated and needing paging."""
    return json.dumps(
        {
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
                                                "oid": "aaa",
                                                "statusCheckRollup": {
                                                    "contexts": _contexts([GATE])
                                                },
                                            }
                                        }
                                    ]
                                },
                            },
                            {
                                "number": 2,
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "oid": "bbb",
                                                "statusCheckRollup": {
                                                    "contexts": _contexts(
                                                        ["x"],
                                                        total=136,
                                                        has_next=True,
                                                        cursor="cur",
                                                    )
                                                },
                                            }
                                        }
                                    ]
                                },
                            },
                        ]
                    }
                }
            }
        }
    )


def _is_page_query(args: list) -> bool:
    """The paging query is the one that does not select `pullRequests`."""
    return "pullRequests" not in args[3]


def test_the_arrival_query_excludes_pull_requests_closed_without_merge():
    """Bug 1. An abandoned branch almost always carries an incomplete check set
    — CI cancelled, or never started for the final SHA — so it reads as a
    conclusive *miss*. And because the sort key is UPDATED_AT and closing a PR
    updates it, abandoning a stale branch actively promotes it into the window
    and evicts a real sample: the verdict became a function of PR hygiene, and
    flapped as the window moved.

    The filter is applied server-side, so the observable surface is the query
    this script sends. Asserting it here is what stops the `states:` clause
    being dropped in a future edit."""
    sent: list = []

    def run(args: list) -> str:
        sent.append(args[3])
        return json.dumps(_arrival_with(_contexts([GATE])))

    fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert "states: [OPEN, MERGED]" in sent[0]


def test_a_truncated_sample_is_resolved_by_paging_not_discarded():
    """Bug 2. The gate sitting past context 100 used to discard the sample
    outright. Both `atlan-application-sdk` bump PRs — the highest-signal PRs in
    a connector repo — carry ~136 contexts, so the highest-signal evidence was
    exactly the evidence being thrown away."""
    calls: list = []

    def run(args: list) -> str:
        calls.append(args)
        if _is_page_query(args):
            return json.dumps(_page(_contexts([GATE, "suite / D001"])))
        return json.dumps(
            _arrival_with(
                _contexts(
                    [f"suite / D{i:03d}" for i in range(100)],
                    total=136,
                    has_next=True,
                    cursor="Y3Vyc29yOjEwMA==",
                )
            )
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": True, "truncated": False}]
    assert sum(1 for a in calls if _is_page_query(a)) == 1


def test_paging_to_exhaustion_turns_truncation_into_a_conclusive_miss():
    """The other half of the same fix: once every page has been read and the
    gate is in none of them, that is real evidence of non-arrival — not an
    unreadable sample. Without this the denominator only ever shrinks."""

    def run(args: list) -> str:
        if _is_page_query(args):
            return json.dumps(_page(_contexts(["suite / D101"], has_next=False)))
        return json.dumps(
            _arrival_with(
                _contexts(["suite / D001"], total=101, has_next=True, cursor="cur")
            )
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": False, "truncated": False}]
    # …and that miss now reaches the verdict, instead of leaving the denominator.
    assert classify_arrival(samples)[:3] == (ARRIVAL_NEVER, 1, 0)


def test_paging_is_bounded_and_an_unfinished_walk_stays_truncated():
    """A connection that never reports exhaustion must not stall the fleet
    sweep. Hitting the cap degrades to the pre-paging behaviour — the sample
    leaves the denominator — rather than to a false miss."""
    pages = 0

    def run(args: list) -> str:
        nonlocal pages
        if _is_page_query(args):
            pages += 1
            return json.dumps(_page(_contexts(["x"], has_next=True, cursor="more")))
        return json.dumps(
            _arrival_with(_contexts(["y"], total=9999, has_next=True, cursor="cur"))
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert pages == MAX_CONTEXT_PAGES
    assert samples == [{"number": 1, "found": False, "truncated": True}]
    assert classify_arrival(samples)[0] == ARRIVAL_UNKNOWN


def test_a_gate_found_on_the_first_page_is_never_paged():
    """Paging is a repair path, not a cost every repo pays. A commit whose first
    page already contains the gate spends no extra API call even when the
    connection is truncated."""
    calls: list = []

    def run(args: list) -> str:
        calls.append(args)
        return json.dumps(
            _arrival_with(_contexts([GATE], total=136, has_next=True, cursor="cur"))
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": True, "truncated": True}]
    assert not any(_is_page_query(a) for a in calls)
    # A truncated sample that found the gate was always conclusive; paging does
    # not change that.
    assert classify_arrival(samples)[:3] == (ARRIVAL_REPORTING, 1, 1)


def test_an_unreadable_arrival_probe_is_a_finding_not_an_empty_findings_list():
    """The silence FND-1947 would otherwise have relocated rather than fixed.

    Paging removes the routine cause of truncation — which also removes the only
    place a human would have noticed truncation, since the dashboard card stops
    mentioning it once the counter sits at zero. So an all-truncated sample now
    reports itself. Without this, a scanner that had stopped working produced a
    record indistinguishable from one with nothing to say: arrival `unknown`,
    `findings: []`."""
    record = _evaluate(
        arrival_samples=[{"found": False, "truncated": True} for _ in range(3)]
    )
    assert record["arrival"]["status"] == ARRIVAL_UNKNOWN
    assert record["arrival"]["prsSampled"] == 0
    assert record["arrival"]["truncatedSamples"] == 3
    assert FINDING_ARRIVAL_UNREADABLE in _finding_ids(record)
    # It is a statement about the probe, never about the repo's CI — so it must
    # not also claim the gate is not arriving.
    assert FINDING_NOT_ARRIVING not in _finding_ids(record)


def test_the_unreadable_probe_finding_is_a_warning_not_an_error():
    """The severity field is what carries "the probe, not the repo", so this is
    not cosmetic. connector-pulse renders an `error` pill red and everything
    else amber (`pages/GateEnforcement.tsx`), and ranks a repo's headline across
    `("error", "warning", "info")`. Emitting `error` here would make an
    unreadable probe pixel-identical to a gate that genuinely never arrives, and
    would promote the repo's headline severity to match — so no wording on
    either side could recover the distinction."""
    record = _evaluate(arrival_samples=[{"found": False, "truncated": True}])
    finding = next(
        f for f in record["findings"] if f["id"] == FINDING_ARRIVAL_UNREADABLE
    )
    assert finding["severity"] == "warning"
    # Every finding that IS a claim about the repo stays `error`.
    record = _evaluate(arrival_samples=[{"found": False, "truncated": False}])
    assert [f["severity"] for f in record["findings"]] == ["error"]


def test_no_arrival_data_at_all_is_not_reported_as_an_unreadable_probe():
    """`no-data` (nothing sampled) and `unknown` (sampled but unreadable) are
    different claims. A repo with no recent pull requests has not exposed a
    scanner bug, and reporting one would fire on every quiet repo."""
    record = _evaluate(arrival_samples=[])
    assert record["arrival"]["status"] == ARRIVAL_NO_DATA
    assert record["findings"] == []


def test_the_unreadable_probe_finding_is_scoped_to_gated_repos():
    """An ungated repo's arrival is moot — `gate-not-required` is the finding
    that matters there, and stacking a probe complaint on top would double-count
    it in the fleet rollup."""
    record = _evaluate(
        rulesets=[],
        arrival_samples=[{"found": False, "truncated": True}],
    )
    assert _finding_ids(record) == {FINDING_NOT_REQUIRED}


def test_paging_stops_when_the_commit_can_no_longer_be_resolved():
    """A force-pushed or GC'd head returns `object: null`. That is a legitimate
    skip, not schema drift, so it must not raise and must not loop — and it is
    flagged `unresolvable` so it cannot be mistaken for an exhausted page."""
    assert parse_contexts_response({"data": {"repository": {"object": None}}}) == {
        "names": set(),
        "mergeGroupNames": set(),
        "count": 0,
        "hasNextPage": False,
        "cursor": None,
        "unresolvable": True,
    }


def test_an_unresolvable_commit_is_not_recorded_as_a_conclusive_miss():
    """An unreadable commit and a fully walked one have the same page shape —
    no names, nothing more to fetch. Reading the second out of the first turns
    a force-pushed head into `never-arriving`, and on a gated repo into a
    `gate-not-arriving` error: a false claim about the repo, which is worse than
    the silence this PR set out to fix.

    Asserted through `fetch_arrival_samples` rather than on the page shape,
    because the page-shape assertion above is exactly what let this through."""

    def run(args: list) -> str:
        if _is_page_query(args):
            return json.dumps({"data": {"repository": {"object": None}}})
        return json.dumps(
            _arrival_with(_contexts(["x"], total=136, has_next=True, cursor="cur"))
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": False, "truncated": True}]
    assert classify_arrival(samples)[0] == ARRIVAL_UNKNOWN
    record = _evaluate(arrival_samples=samples)
    assert FINDING_NOT_ARRIVING not in _finding_ids(record)
    assert FINDING_ARRIVAL_UNREADABLE in _finding_ids(record)


def test_one_failed_page_does_not_discard_the_other_samples():
    """Paging is the only per-sample request in this probe, so it is the only
    place one transient failure can take the others with it. Letting a `GhError`
    escape `fetch_arrival_samples` sends `scan_repo` to `samples = None`, and a
    sibling that had already conclusively found the gate dies with it — the repo
    lands on `no-data` with no findings, which is the same silence
    `gate-arrival-unreadable` exists to close, one layer below it."""

    def run(args: list) -> str:
        if _is_page_query(args):
            raise GhError("gh api failed: HTTP 502", status=502)
        return _arrival_two_prs()

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [
        {"number": 1, "found": True, "truncated": False},
        {"number": 2, "found": False, "truncated": True},
    ]
    # PR #1's evidence survives, so the repo still has a verdict.
    assert classify_arrival(samples)[:3] == (ARRIVAL_REPORTING, 1, 1)


def test_a_repo_whose_every_page_fails_is_unknown_and_reported_not_silent():
    """The degenerate case of the above: nothing conclusive survives. That must
    be `unknown` + a finding, never `no-data` + an empty findings list."""

    def run(args: list) -> str:
        if _is_page_query(args):
            raise GhError("gh api failed: HTTP 502", status=502)
        return json.dumps(
            _arrival_with(_contexts(["x"], total=136, has_next=True, cursor="cur"))
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": False, "truncated": True}]
    record = _evaluate(arrival_samples=samples)
    assert record["arrival"]["status"] == ARRIVAL_UNKNOWN
    assert FINDING_ARRIVAL_UNREADABLE in _finding_ids(record)


def test_a_malformed_arrival_body_still_fails_the_whole_probe():
    """Per-sample isolation is scoped to *paging*, which is a network call per
    sample. A malformed top-level body is schema drift and must still reach
    `scan_repo` as a GhError, so the repo degrades to `unknown` rather than
    being evaluated on whatever parsed."""

    def run(args: list) -> str:
        return json.dumps({"data": {"repository": None}})

    with pytest.raises(GhError, match="malformed arrival payload"):
        fetch_arrival_samples(REPO, "main", 5, GATE, run=run)


def test_a_missing_cursor_leaves_the_sample_truncated_rather_than_paging_blind():
    """`hasNextPage` without an `endCursor` gives nothing to page with. The
    sample keeps the old treatment — excluded, never a false miss."""

    def run(args: list) -> str:
        assert not _is_page_query(args), "must not page without a cursor"
        return json.dumps(
            _arrival_with(_contexts(["x"], total=136, has_next=True, cursor=None))
        )

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": False, "truncated": True}]


def test_page_info_outranks_the_count_comparison_but_the_fallback_survives():
    """`pageInfo.hasNextPage` is the authoritative answer; the totalCount
    comparison remains for a payload that did not select it, so the repeated-
    context reasoning above still holds."""
    with_page_info = parse_arrival_nodes(
        _arrival_with(_contexts([GATE] * 3, total=99, has_next=True, cursor="c")), GATE
    )[0]
    assert with_page_info["truncated"] is True  # despite 3 < 99 being unread

    no_page_info = parse_arrival_nodes(
        _arrival_with({"totalCount": 140, "nodes": [_ctx("x")]}), GATE
    )[0]
    assert no_page_info["truncated"] is True
    assert no_page_info["walks"][0]["cursor"] is None


@pytest.mark.parametrize(
    "bad,match",
    [
        pytest.param({"hasNextPage": "yes"}, "hasNextPage", id="hasNextPage"),
        pytest.param(
            {"hasNextPage": True, "endCursor": 7}, "endCursor", id="endCursor"
        ),
    ],
)
def test_a_wrong_typed_page_info_leaf_raises_rather_than_guessing(bad, match):
    """Same fail-loud contract as the other leaves: a wrong-typed paging field
    must reach `scan_repo` as a GhError (arrival `unknown`), never be coerced
    into "nothing more to read" — which would read as a clean conclusive miss."""
    payload = _arrival_with({"totalCount": 136, "pageInfo": bad, "nodes": [_ctx("x")]})
    with pytest.raises(GhError, match=match):
        parse_arrival_nodes(payload, GATE)


def test_an_oid_that_is_not_a_string_raises_rather_than_paging_on_it():
    payload = _arrival_with(_contexts(["x"], total=136, has_next=True, cursor="c"))
    payload["data"]["repository"]["pullRequests"]["nodes"][0]["commits"]["nodes"][0][
        "commit"
    ]["oid"] = 12345
    with pytest.raises(GhError, match="oid"):
        parse_arrival_nodes(payload, GATE)


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


# --- FND-1973: stale and draft pull requests are not evidence ---------------
#
# `atlan-postgres-app` reported arrival `intermittent` — and therefore NOT
# BASELINED on connector-pulse — on a repo whose gate was wired correctly and
# green on every real pull request. The missing sighting was #501: a draft, open
# and conflicted for six weeks, whose head commit predated the repo's adoption
# of the unified tests.yaml and so carried the legacy check set. Something
# bumped its `updatedAt`, which promoted it into the 5-PR window and evicted a
# real sample.

WORKFLOW_CHANGED = "2026-08-31T14:10:55Z"
AFTER = "2026-09-04T05:26:42Z"
BEFORE = "2026-07-31T09:12:00Z"


def _pr(number, *, found=True, draft=False, committed=AFTER, oid=None) -> dict:
    """One pull request node in the arrival query's shape."""
    return {
        "number": number,
        "isDraft": draft,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": oid or f"sha{number}",
                        "committedDate": committed,
                        "statusCheckRollup": {
                            "contexts": _contexts(
                                [GATE] if found else ["legacy / unit"]
                            )
                        },
                    }
                }
            ]
        },
    }


def _arrival_nodes(*pr_nodes) -> str:
    return json.dumps(
        {"data": {"repository": {"pullRequests": {"nodes": list(pr_nodes)}}}}
    )


def _paged_arrival(*pr_nodes):
    """A stub that honours `first`, like the server does.

    Without this the fixture hands back every pull request it was given
    whatever page size was asked for, and the over-fetch becomes untestable:
    every exclusion looks free because the pool was never bounded.
    """

    def run(args: list) -> str:
        first = next(
            int(a.split("=", 1)[1]) for a in args if str(a).startswith("first=")
        )
        return _arrival_nodes(*pr_nodes[:first])

    return run


def test_the_arrival_query_selects_draft_ness_and_the_head_commit_date():
    """Both filters are client-side — the `pullRequests` connection has no
    draft or date argument — so the query must actually *select* the two fields
    they read. Dropping either from the selection silently disables the filter
    rather than failing, which is why it is pinned here."""
    sent: list = []

    def run(args: list) -> str:
        sent.append(args[3])
        return _arrival_nodes(_pr(1))

    fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert "isDraft" in sent[0]
    assert "committedDate" in sent[0]


def test_the_postgres_incident_does_not_recur():
    """The whole issue in one case. A stale draft sits at the top of the window
    (its `updatedAt` was bumped by a comment), five healthy pull requests sit
    behind it. Before the fix the draft was sampled and read as a conclusive
    miss: 4/5 found, arrival `intermittent`, a `gate-not-arriving` error, and a
    repo with perfectly good CI reported as not baselined."""

    def run(args: list) -> str:
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}]])
        if args[1] == f"repos/{REPO}/rulesets/1":
            return json.dumps(_ruleset())
        if args[1].startswith(f"repos/{REPO}/commits?path="):
            return json.dumps({"d": WORKFLOW_CHANGED})
        if args[1] == "graphql":
            return _paged_arrival(
                # #501 itself: a draft whose head predates the workflow.
                _pr(501, found=False, draft=True, committed=BEFORE),
                # A draft opened minutes ago, gate not reported yet. Fresh, so
                # only the draft rule excludes it.
                _pr(596, found=False, draft=True),
                # A merged pull request from before the workflow changed. Not a
                # draft, so only the staleness rule excludes it — draft-ness was
                # never what broke this.
                _pr(540, found=False, committed=BEFORE),
                *(_pr(n) for n in range(595, 590, -1)),
            )(args)
        return json.dumps("sha")

    record = scan_repo(REPO, GATE, sample_size=5, run=run)
    assert record["arrival"]["status"] == ARRIVAL_REPORTING
    assert record["arrival"]["prsSampled"] == 5
    assert record["arrival"]["prsWithContext"] == 5
    assert _finding_ids(record) == set()


def test_an_open_draft_is_never_sampled():
    """Policy call from the review of the incident: a draft influences no
    Fleet-Drift dimension. A merged pull request is never draft, so this only
    ever drops open ones."""
    samples = parse_arrival_nodes(
        json.loads(_arrival_nodes(_pr(1, draft=True), _pr(2))), GATE
    )
    assert [s["number"] for s in samples] == [2]


def test_a_head_commit_predating_the_workflow_is_not_evidence():
    """The real fix. Draft-ness is not what broke this — staleness is, and a
    stale *non-draft* pull request poisons the sample identically. A commit that
    predates the current tests.yaml ran different CI wiring and cannot be
    evidence about the wiring in place now."""
    samples = parse_arrival_nodes(
        json.loads(
            _arrival_nodes(
                _pr(1, found=False, committed=BEFORE),
                _pr(2),
            )
        ),
        GATE,
    )
    selected = select_arrival_samples(samples, WORKFLOW_CHANGED, 5)
    assert [s["number"] for s in selected] == [2]


def test_a_head_commit_from_the_cutoff_instant_itself_still_counts():
    """Boundary: the commit that *changed* the workflow is evidence about it.
    Only strictly-older heads are excluded."""
    samples = parse_arrival_nodes(
        json.loads(_arrival_nodes(_pr(1, committed=WORKFLOW_CHANGED))), GATE
    )
    assert len(select_arrival_samples(samples, WORKFLOW_CHANGED, 5)) == 1


def test_exclusions_shrink_the_candidate_pool_not_the_denominator():
    """Over-fetching is what makes (1) and (2) safe. Filtering 5 fetched samples
    down to 4 would repeat the mistake FND-1947 fixed for CLOSED-without-merge
    pull requests: abandoned work shrinking the evidence base rather than being
    stepped over."""

    run = _paged_arrival(
        _pr(1, found=False, draft=True),
        _pr(2, found=False, committed=BEFORE),
        *(_pr(n) for n in range(3, 9)),
    )
    samples = fetch_arrival_samples(
        REPO, "main", 5, GATE, run=run, stale_before=WORKFLOW_CHANGED
    )
    assert len(samples) == 5
    assert all(s["found"] for s in samples)
    assert classify_arrival(samples)[0] == ARRIVAL_REPORTING


def test_the_over_fetch_is_bounded():
    """`first` is a server-side page and GitHub caps it at 100; an unbounded
    multiplier would also make a fleet sweep proportionally slower for samples
    it discards."""
    sent: list = []

    def run(args: list) -> str:
        sent.append(args)
        return _arrival_nodes(_pr(1))

    fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert f"first={5 * ARRIVAL_OVERFETCH}" in sent[0]

    sent.clear()
    fetch_arrival_samples(REPO, "main", 500, GATE, run=run)
    assert f"first={MAX_ARRIVAL_FETCH}" in sent[0]


def test_an_excluded_sample_costs_no_paging_request():
    """Selection runs before paging. A discarded sample must not spend the one
    follow-up request per truncated commit that paging costs — across ~80 repos
    that is the difference between a bounded sweep and a wasteful one."""
    calls: list = []

    def run(args: list) -> str:
        calls.append(args)
        if _is_page_query(args):
            return json.dumps(_page(_contexts([GATE])))
        return _arrival_nodes(
            {
                "number": 501,
                "isDraft": True,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": "stale",
                                "committedDate": BEFORE,
                                "statusCheckRollup": {
                                    "contexts": _contexts(
                                        ["legacy / unit"],
                                        total=136,
                                        has_next=True,
                                        cursor="cur",
                                    )
                                },
                            }
                        }
                    ]
                },
            },
            _pr(595),
        )

    fetch_arrival_samples(REPO, "main", 5, GATE, run=run, stale_before=WORKFLOW_CHANGED)
    assert not any(_is_page_query(a) for a in calls)


def test_a_repo_with_no_pull_requests_since_the_change_is_no_data():
    """The honest residue. Excluding every sample leaves `no-data` — "nothing
    has run since the workflow changed" — which emits no finding. That is
    strictly better than the false `never-arriving` those same samples would
    produce, and it is why the filter is safe to apply bluntly."""

    run = _paged_arrival(*(_pr(n, found=False, committed=BEFORE) for n in (1, 2)))
    samples = fetch_arrival_samples(
        REPO, "main", 5, GATE, run=run, stale_before=WORKFLOW_CHANGED
    )
    record = _evaluate(arrival_samples=samples)
    assert record["arrival"]["status"] == ARRIVAL_NO_DATA
    assert _finding_ids(record) == set()


def test_no_cutoff_means_no_exclusion():
    """A repo can produce the gate context from a differently-named workflow, so
    a missing tests.yaml is not a reason to discard its pull requests. A filter
    that cannot establish its own boundary must not guess at one."""
    samples = parse_arrival_nodes(
        json.loads(_arrival_nodes(_pr(1, committed=BEFORE), _pr(2))), GATE
    )
    assert len(select_arrival_samples(samples, None, 5)) == 2


def test_a_missing_tests_workflow_yields_no_cutoff():
    def run(args: list) -> str:
        assert args[1].startswith(f"repos/{REPO}/commits?path=")
        return json.dumps({"d": None})

    assert fetch_tests_workflow_last_modified(REPO, run=run) is None


def test_the_cutoff_is_the_last_change_not_the_first():
    """`per_page=1` on a commit listing is newest-first, so the value read is
    the most recent change to the file. The adoption commit is not enough: on
    `atlan-postgres-app` #501's head postdated the file's introduction and still
    predated the migration to the SDK reusable that produces the gate."""
    sent: list = []

    def run(args: list) -> str:
        sent.append(args[1])
        return json.dumps({"d": WORKFLOW_CHANGED})

    assert fetch_tests_workflow_last_modified(REPO, run=run) == WORKFLOW_CHANGED
    assert "per_page=1" in sent[0]
    assert "path=.github/workflows/tests.yaml" in sent[0]


def test_an_unreadable_cutoff_disables_the_filter_and_nothing_else():
    """Guarded like every other corroborating read. Losing the cutoff must not
    fail the arrival probe, and must not make the filter stricter — the
    degraded behaviour is the pre-FND-1973 one."""

    def run(args: list) -> str:
        if args[1] == f"repos/{REPO}":
            return json.dumps({"b": "main"})
        if args[1].startswith(f"repos/{REPO}/rulesets?"):
            return json.dumps([[{"id": 1}]])
        if args[1] == f"repos/{REPO}/rulesets/1":
            return json.dumps(_ruleset())
        if args[1].startswith(f"repos/{REPO}/commits?path="):
            raise GhError("gh api failed: HTTP 502", status=502)
        if args[1] == "graphql":
            return _arrival_nodes(_pr(1), _pr(2, committed=BEFORE))
        return json.dumps("sha")

    record = scan_repo(REPO, GATE, sample_size=5, run=run)
    assert record["status"] == STATUS_GATED
    assert record["arrival"]["status"] == ARRIVAL_REPORTING
    assert record["arrival"]["prsSampled"] == 2


def test_an_unparseable_timestamp_raises_rather_than_skipping_the_filter():
    """The one place a `None` fallback would be wrong: a value we *did* receive
    but cannot read. Silently treating it as "no cutoff" restores the bug."""
    samples = parse_arrival_nodes(json.loads(_arrival_nodes(_pr(1))), GATE)
    with pytest.raises(GhError, match="unparseable"):
        select_arrival_samples(samples, "last tuesday", 5)


@pytest.mark.parametrize(
    "pr_node, match",
    [
        pytest.param(
            {"number": 1, "isDraft": "yes", "commits": {"nodes": []}},
            "isDraft",
            id="isDraft",
        ),
        pytest.param(
            {
                "number": 1,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": 1234,
                                "statusCheckRollup": {"contexts": _contexts([GATE])},
                            }
                        }
                    ]
                },
            },
            "committedDate",
            id="committedDate",
        ),
    ],
)
def test_a_wrong_typed_filter_leaf_raises_rather_than_being_ignored(pr_node, match):
    """Both new leaves follow the module's fail-loud contract. A wrong-typed
    `isDraft` is truthy-but-not-boolean (`"false"` included), and a wrong-typed
    `committedDate` would compare against a datetime and raise a TypeError deep
    in the walk — uncaught, aborting the whole sweep."""
    with pytest.raises(GhError, match=match):
        parse_arrival_nodes(_arrival_payload(pr_node), GATE)


# --- FND-2783: a merged PR gated only by its merge_group run ----------------
#
# `atlan-bw-app` and `atlan-cognos-app` reported arrival `intermittent` (4/5) on
# gates that were wired and passing. The miss in each was a stale Renovate PR
# the fleet bot force-pushed and queued a second later: no `pull_request` run of
# tests.yaml started for the new head, the `merge_group` run passed on the SHA
# that became the merge commit, and the PR merged. The scanner read the head
# only. The merge commit also carries the `push`-to-main run of tests.yaml under
# the same context name — which proves nothing about the PR — so only a
# `merge_group` sighting on it may count.


def _run_ctx(name: str, event) -> dict:
    """A check-run context with the workflow run's triggering event."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "checkSuite": {"workflowRun": {"event": event}},
    }


def _run_contexts(ctxs, *, total=None, has_next=False, cursor=None) -> dict:
    return {
        "totalCount": len(ctxs) if total is None else total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": list(ctxs),
    }


def _merged(number, merge_contexts, *, head_found=False, merge_oid=None) -> dict:
    """A merged PR node: its head as `_pr` builds it, plus a merge commit."""
    node = _pr(number, found=head_found)
    node["mergeCommit"] = {
        "oid": merge_oid or f"merge{number}",
        "statusCheckRollup": {"contexts": merge_contexts},
    }
    return node


def _truncated_merge_arrival(page_contexts: dict):
    """A stub: one merged PR whose head misses and whose merge commit spills
    past page one; every paging request returns ``page_contexts``."""
    paged_oids: list = []

    def run(args: list) -> str:
        if _is_page_query(args):
            paged_oids.append(next(a for a in args if a.startswith("oid=")))
            return json.dumps(_page(page_contexts))
        return _arrival_nodes(
            _merged(
                1,
                _run_contexts(
                    [_run_ctx("x", "merge_group")], total=136, has_next=True, cursor="c"
                ),
                merge_oid="m1",
            )
        )

    return run, paged_oids


# The bw-app #138 merge commit, trimmed: the gate twice under one name, once per
# triggering event.
_QUEUED_MERGE = [
    _run_ctx("Release Gate", "merge_group"),
    _run_ctx(GATE, "merge_group"),
    _run_ctx(GATE, "push"),
]


def test_the_merge_queue_incident_does_not_recur():
    """Red-green for the ticket: the same five PRs are `intermittent` when the
    merge commit is ignored and `reporting` when it is read."""
    others = [_pr(n) for n in (2, 3, 4, 5)]
    incident = _merged(1, _run_contexts(_QUEUED_MERGE))

    blind = {k: v for k, v in incident.items() if k != "mergeCommit"}
    red = fetch_arrival_samples(
        REPO, "main", 5, GATE, run=_paged_arrival(blind, *others)
    )
    assert classify_arrival(red)[0] == ARRIVAL_INTERMITTENT

    green = fetch_arrival_samples(
        REPO, "main", 5, GATE, run=_paged_arrival(incident, *others)
    )
    assert green[0] == {"number": 1, "found": True, "truncated": False}
    assert classify_arrival(green)[0] == ARRIVAL_REPORTING


def test_a_push_run_on_the_merge_commit_does_not_count():
    """The `push` run starts after the merge, so it cannot have gated the PR.
    Accepting the bare context name would make every merged PR on a repo whose
    tests.yaml runs on push read `found`, whatever gated it."""
    node = _merged(1, _run_contexts([_run_ctx(GATE, "push")]))
    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=_paged_arrival(node))
    assert samples == [{"number": 1, "found": False, "truncated": False}]


@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param({"__typename": "StatusContext", "context": GATE}, id="status"),
        pytest.param(
            {"__typename": "CheckRun", "name": GATE, "checkSuite": None}, id="no-suite"
        ),
        pytest.param(
            {
                "__typename": "CheckRun",
                "name": GATE,
                "checkSuite": {"workflowRun": None},
            },
            id="non-actions-suite",
        ),
    ],
)
def test_a_merge_commit_context_without_a_merge_group_run_does_not_count(ctx):
    """Anything that cannot prove a `merge_group` run is not a sighting."""
    node = _merged(1, _run_contexts([ctx]))
    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=_paged_arrival(node))
    assert samples[0]["found"] is False


def test_a_merged_pr_with_no_gate_on_either_commit_is_still_a_miss():
    node = _merged(1, _run_contexts([_run_ctx("Release Gate", "merge_group")]))
    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=_paged_arrival(node))
    assert samples == [{"number": 1, "found": False, "truncated": False}]
    assert classify_arrival(samples)[0] == ARRIVAL_NEVER


def test_an_open_pr_still_reads_the_head_only():
    """GitHub returns `mergeCommit: null` on an open PR; the head's reading
    stands, and no second walk is created."""
    node = {**_pr(1, found=False), "mergeCommit": None}
    parsed = parse_arrival_nodes(json.loads(_arrival_nodes(node)), GATE)
    assert parsed[0]["found"] is False
    assert [w["key"] for w in parsed[0]["walks"]] == ["names"]


def test_a_merge_commit_with_no_checks_leaves_the_head_reading():
    node = _pr(1, found=False)
    node["mergeCommit"] = {"oid": "m", "statusCheckRollup": None}
    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=_paged_arrival(node))
    assert samples == [{"number": 1, "found": False, "truncated": False}]


def test_the_arrival_queries_select_the_merge_commit_and_the_run_event():
    """Both are read client-side, so dropping either from the selection silently
    restores the bug — pinned here, like `isDraft` and `committedDate`. The event
    must be on the paging query too, or a merge commit past 100 contexts can
    never be resolved as found."""
    sent: list = []
    stub, _ = _truncated_merge_arrival(_run_contexts([_run_ctx(GATE, "merge_group")]))

    def run(args: list) -> str:
        sent.append(args[3])
        return stub(args)

    fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    arrival, page = sent
    assert "mergeCommit" in arrival
    assert "workflowRun { event }" in arrival
    assert "workflowRun { event }" in page


def test_a_truncated_merge_commit_is_paged_for_a_merge_group_sighting():
    """bw-app's merge commit carried 85 contexts; busier repos pass 100, so the
    merge commit needs the same paging as the head (FND-1947). Only the walk
    that was cut off is paged — the head here fit on one page."""
    run, paged_oids = _truncated_merge_arrival(
        _run_contexts([_run_ctx(GATE, "merge_group")])
    )
    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": True, "truncated": False}]
    assert paged_oids == ["oid=m1"]


def test_a_push_sighting_on_a_later_merge_commit_page_does_not_count():
    """The event filter holds on every page, not just the first. Every
    truncated walk exhausted without a sighting is a conclusive miss."""
    run, _ = _truncated_merge_arrival(_run_contexts([_run_ctx(GATE, "push")]))
    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=run)
    assert samples == [{"number": 1, "found": False, "truncated": False}]


def test_an_unfinished_merge_commit_walk_keeps_the_sample_out_of_the_denominator():
    """A merge commit GitHub can no longer resolve has not been ruled out, so a
    head miss alone must not become a conclusive miss."""
    run, _ = _truncated_merge_arrival(None)

    def unresolvable(args: list) -> str:
        if _is_page_query(args):
            return json.dumps({"data": {"repository": {"object": None}}})
        return run(args)

    samples = fetch_arrival_samples(REPO, "main", 5, GATE, run=unresolvable)
    assert samples == [{"number": 1, "found": False, "truncated": True}]


@pytest.mark.parametrize(
    "ctx, match",
    [
        pytest.param(
            {"__typename": "CheckRun", "name": GATE, "checkSuite": "x"},
            "checkSuite",
            id="checkSuite",
        ),
        pytest.param(
            {"__typename": "CheckRun", "name": GATE, "checkSuite": {"workflowRun": 1}},
            "workflowRun",
            id="workflowRun",
        ),
        pytest.param(_run_ctx(GATE, 7), "event", id="event"),
    ],
)
def test_a_wrong_typed_run_event_raises_rather_than_being_ignored(ctx, match):
    """Same fail-loud contract as every other leaf: drift reaches `scan_repo`
    as a GhError (arrival `unknown`), never as a silent non-sighting."""
    node = _merged(1, _run_contexts([ctx]))
    with pytest.raises(GhError, match=match):
        parse_arrival_nodes(json.loads(_arrival_nodes(node)), GATE)


def test_a_wrong_typed_merge_commit_oid_raises():
    node = _merged(1, _run_contexts([]))
    node["mergeCommit"]["oid"] = 12345
    with pytest.raises(GhError, match="mergeCommit.oid"):
        parse_arrival_nodes(json.loads(_arrival_nodes(node)), GATE)
