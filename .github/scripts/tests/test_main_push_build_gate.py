"""Build & Publish skips the image build on a non-SDR main push (FND-3327).

A push to main that will not publish built an image nobody consumed: the
marketplace publish comes from the release event, which rebuilds. Only an SDR
deploy-on-merge app (`self_deployed_runtime: true`) ships the main-push image.

`build-and-publish-app.yaml` now carries a `build-decision` job that reads
`enable_sdr` from atlan.yaml on exactly that case, and the expensive jobs skip
on exactly `enable_sdr == 'false'`. Every gate is evaluated here through the
real expressions, in both directions:

* `build-decision` runs ONLY on a push to main that does not publish, so no
  other event (release, dispatch, branch build, legacy publish-on-push) can
  ever produce a skip;
* the gated jobs skip ONLY on the answer 'false', and run on 'true' and on the
  empty output a skipped `build-decision` leaves behind;
* everything else in the build chain waits on `prepare`, so skipping it skips
  the chain;
* the nested security scan is blocking on exactly a non-publishing main run.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _gha_expr import evaluate, evaluate_operand  # noqa: E402
from parse_atlan_yaml import parse  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-and-publish-app.yaml"

#: `!failure()` cannot be evaluated statically (see _gha_expr). Every gated job
#: must keep it as a literal prefix — a failed `build-decision` (invalid
#: atlan.yaml) has to stop the chain, not let it run — and the remainder, which
#: carries the gate's meaning, is what gets evaluated.
FAILURE_PREFIX = "!failure() && "

GATED_JOBS = ("certify-unit", "certify", "leak-scan", "prepare")
#: Jobs that only run behind `prepare`, directly or through `merge`.
CHAIN_JOBS = ("build", "merge", "security-scan", "app-deployment-dispatcher", "publish")

MAIN = "refs/heads/main"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _gate_body(expression: str) -> str:
    source = expression.strip()
    if source.startswith("${{") and source.endswith("}}"):
        source = source[3:-2].strip()
    assert source.startswith(FAILURE_PREFIX), source
    return source[len(FAILURE_PREFIX) :]


@pytest.mark.parametrize(
    ("event", "ref", "publish", "runs"),
    [
        ("push", MAIN, False, True),
        # Legacy callers pass publish=true on push: they keep building.
        ("push", MAIN, True, False),
        ("push", "refs/heads/feature-x", False, False),
        ("release", "refs/tags/v1.2.3", True, False),
        ("release", MAIN, False, False),
        ("workflow_dispatch", MAIN, False, False),
        ("workflow_dispatch", MAIN, True, False),
        ("pull_request", "refs/pull/1/merge", False, False),
    ],
)
def test_build_decision_runs_only_on_a_non_publishing_main_push(
    event: str, ref: str, publish: bool, runs: bool
) -> None:
    job = _jobs()["build-decision"]
    contexts = {
        "github": {"event_name": event, "ref": ref},
        "inputs": {"publish": publish},
    }
    assert evaluate(job["if"], contexts) is runs


def test_build_decision_exposes_enable_sdr_from_the_shared_parser() -> None:
    job = _jobs()["build-decision"]
    assert job["outputs"] == {"enable_sdr": "${{ steps.manifest.outputs.enable_sdr }}"}
    (step,) = [s for s in job["steps"] if s.get("id") == "manifest"]
    assert step["run"].strip().endswith(".github/scripts/parse_atlan_yaml.py")


@pytest.mark.parametrize(
    ("sdr_line", "expected"), [("", "false"), ("self_deployed_runtime: true\n", "true")]
)
def test_parser_answers_exactly_true_or_false(
    tmp_path: Path, sdr_line: str, expected: str
) -> None:
    # The gates compare against the literal 'false'; any other spelling of
    # "no SDR" would silently keep building.
    atlan_yaml = tmp_path / "atlan.yaml"
    atlan_yaml.write_text(f"name: example-app\napp_id: example\n{sdr_line}")
    out = parse(str(atlan_yaml), str(tmp_path / "uv.lock"))
    assert out["enable_sdr"] == expected


@pytest.mark.parametrize("job_id", GATED_JOBS)
def test_gated_jobs_wait_on_build_decision(job_id: str) -> None:
    needs = _jobs()[job_id]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "build-decision" in needs


@pytest.mark.parametrize("job_id", GATED_JOBS)
@pytest.mark.parametrize("answer", ["", "true", "false"])
def test_gated_jobs_skip_only_on_false(job_id: str, answer: str) -> None:
    # Main push, publish off: the one case build-decision runs on. certify and
    # leak-scan additionally require publish-or-main, which holds here.
    body = _gate_body(_jobs()[job_id]["if"])
    contexts = {
        "github": {"ref": MAIN},
        "inputs": {"publish": False},
        "needs": {"build-decision": {"outputs": {"enable_sdr": answer}}},
    }
    assert evaluate(body, contexts) is (answer != "false")


@pytest.mark.parametrize("job_id", ("certify-unit", "certify", "leak-scan"))
@pytest.mark.parametrize(
    ("ref", "publish", "runs"),
    [
        (MAIN, False, True),
        ("refs/tags/v1.2.3", True, True),
        ("refs/heads/feature-x", False, False),
    ],
)
def test_certify_and_leak_scan_keep_their_publish_or_main_condition(
    job_id: str, ref: str, publish: bool, runs: bool
) -> None:
    # With build-decision skipped (empty output), behaviour is unchanged.
    body = _gate_body(_jobs()[job_id]["if"])
    contexts = {
        "github": {"ref": ref},
        "inputs": {"publish": publish},
        "needs": {"build-decision": {"outputs": {"enable_sdr": ""}}},
    }
    assert evaluate(body, contexts) is runs


@pytest.mark.parametrize(("shards", "runs"), [(1, False), (2, True), (10, True)])
def test_shard_legs_run_only_when_the_app_opts_in(shards: int, runs: bool) -> None:
    # Sharding is opt-in: at the default (1) certify runs the tests itself and
    # an app pays for no extra jobs.
    body = _gate_body(_jobs()["certify-unit"]["if"])
    contexts = {
        "github": {"ref": MAIN},
        "inputs": {"publish": True, "unit_test_shards": shards},
        "needs": {"build-decision": {"outputs": {"enable_sdr": ""}}},
    }
    assert evaluate(body, contexts) is runs


def _shard_matrix() -> tuple[dict[str, list[int]], str]:
    """Split certify-unit's matrix expression into its lookup table and index."""
    expression = _jobs()["certify-unit"]["strategy"]["matrix"]["shard"]
    match = re.fullmatch(
        r"\$\{\{ fromJSON\('(?P<table>[^']*)'\)\[(?P<index>.*)\] \}\}", expression
    )
    assert match, expression
    return json.loads(match["table"]), match["index"]


def test_shard_matrix_has_one_leg_per_shard() -> None:
    # The leg list is looked up from a table, not built by a planning job
    # (one fewer billed job). Every count 1-10 maps to legs 1..N.
    table, index = _shard_matrix()
    assert index == "format('{0}', inputs.unit_test_shards)"
    assert table == {str(n): list(range(1, n + 1)) for n in range(1, 11)}


def test_shard_matrix_has_no_entry_outside_the_range() -> None:
    # No entry -> empty matrix -> certify-unit fails -> certify/publish skip.
    table, _ = _shard_matrix()
    assert not {"0", "-1", "11"} & table.keys()


def test_shard_legs_cannot_hand_app_code_a_write_token() -> None:
    # The legs run app code (pytest): the job token is read-only, and no
    # checkout persists it into .git/config where that code could read it.
    job = _jobs()["certify-unit"]
    assert job["permissions"] == {"contents": "read"}
    checkouts = [s for s in job["steps"] if "actions/checkout@" in s.get("uses", "")]
    assert checkouts
    for step in checkouts:
        assert step["with"].get("persist-credentials") is False, step["name"]


def test_certify_itself_does_not_depend_on_the_shard_input() -> None:
    assert "unit_test_shards" not in _jobs()["certify"]["if"]


def test_sharding_defaults_to_off() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as boolean True.
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert inputs["unit_test_shards"]["default"] == 1


@pytest.mark.parametrize("job_id", CHAIN_JOBS)
def test_build_chain_sits_behind_prepare(job_id: str) -> None:
    jobs = _jobs()
    needs = jobs[job_id]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "prepare" in needs


@pytest.mark.parametrize(
    ("ref", "publish", "release_tag", "promote_source", "blocking"),
    [
        (MAIN, False, "", "", True),
        (MAIN, True, "", "", False),
        # FND-3328: a release that promoted the scanned bump-PR candidate is
        # report-only; one that had to rebuild is gated by this scan.
        ("refs/tags/v1.2.3", True, "v1.2.3", "ghcr.io/atlanhq/x@sha256:ab", False),
        ("refs/tags/v1.2.3", True, "v1.2.3", "", True),
        ("refs/heads/feature-x", False, "", "", False),
    ],
)
def test_security_scan_blocks_on_a_non_publishing_main_run(
    ref: str, publish: bool, release_tag: str, promote_source: str, blocking: bool
) -> None:
    expression = _jobs()["security-scan"]["with"]["fail_on_findings"]
    contexts = {
        "github": {"ref": ref},
        "inputs": {"publish": publish, "release_tag": release_tag},
        "needs": {"prepare": {"outputs": {"promote_source": promote_source}}},
    }
    assert evaluate_operand(expression, contexts) is blocking
