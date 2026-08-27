"""The connector failure path leaves a redacted evidence bundle behind (FND-243).

``LogCollector`` has existed on ``testing/e2e/logs.py`` since the K8s harness
landed and is called from nowhere, so until now a failed full-DAG run collected
no evidence at all. FND-243 wires collection in — but not by calling that
collector, and the correction is worth pinning rather than leaving in a PR
description.

**There is no cluster to collect from on this path.** The connector e2e leg runs
a Docker-compose container on the runner against a remote tenant; nothing in
``e2e-full-reusable.yaml`` or the shared ``sdr-e2e`` composite establishes a
``kubectl`` route into the tenant vcluster. That is the same constraint that
makes the AE submit the only tenant-facing probe of the installed app pod. So
the pod half of an evidence bundle is not collectable here, and what a failed
connector leg can leave behind is the AE run's identity, the per-node table, the
Atlas readings and the traceback — which is what it now does. The local
container's log is dumped into the same ``results/`` directory by the CI action,
so the two arrive in one artifact.

The directory is the other half of the wiring, and it is why this needs no
workflow change: ``results/`` is already where ``upload-artifact`` is pointed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.e2e import BaseE2ETest
from application_sdk.testing.e2e.base import FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness.evidence import PLACEHOLDER

_CONN_QN = "default/postgres/1700000000042"


class _Suite(BaseE2ETest):
    connector_short_name = "postgres"
    argo_package_name = "@atlan/postgres"
    argo_template_name = "postgres"


def _suite(tmp_path: Path) -> _Suite:
    """A suite with just the attributes the evidence builder reads.

    Built by hand rather than through ``setup_method``, which needs a tenant in
    the environment. What is under test is what the builder puts in the bundle,
    not how the suite got its identity.
    """
    suite = _Suite()
    suite.evidence_dir = str(tmp_path)
    suite.run_id = 1_700_000_000
    suite.connection_qualified_name = _CONN_QN
    suite._node_dispatch = {}
    suite._expected_node_identities = {}
    suite._seed_version = 1_700_000_099
    return suite


def _outcome(*, failed: bool = True) -> FullDAGOutcome:
    return FullDAGOutcome(
        ae_result=DAGRunResult(
            run_id="ae-run-1",
            workflow_slug="postgres-e2e-1",
            status=DAGRunStatus.FAILED if failed else DAGRunStatus.SUCCEEDED,
            nodes=[
                DAGNodeResult(
                    name="extract",
                    status=DAGNodeStatus.FAILED,
                    started_at_ms=1_000,
                    completed_at_ms=3_000,
                    error_message="auth failed for password=hunter2",
                ),
            ],
        ),
        connection_qualified_name=_CONN_QN,
        connection_in_atlas=False,
        asset_counts={"Table": 0},
        total_assets=0,
        lineage_present=False,
        asset_qn_samples={},
        connection_expected=True,
    )


def _written(tmp_path: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


# ---------------------------------------------------------------------------
# What lands
# ---------------------------------------------------------------------------


def test_a_failed_run_leaves_a_bundle_under_the_evidence_dir(tmp_path: Path) -> None:
    suite = _suite(tmp_path)

    suite._collect_failure_evidence(AssertionError("Full-DAG e2e failed"), _outcome())

    report = json.loads(
        (tmp_path / "_Suite" / "report.json").read_text(encoding="utf-8")
    )
    assert report["readings"]["ae_run_id"] == "ae-run-1"
    assert report["readings"]["connector"] == "postgres"
    assert report["findings"][0]["subject"] == "AssertionError"


def test_the_per_node_table_lands_machine_readable(tmp_path: Path) -> None:
    """The single most-read part of a failed leg, so it goes in the half a script
    can group on rather than only into the rendered text beside it."""
    suite = _suite(tmp_path)

    suite._collect_failure_evidence(AssertionError("nope"), _outcome())

    report = json.loads(
        (tmp_path / "_Suite" / "report.json").read_text(encoding="utf-8")
    )
    assert report["readings"]["dag_nodes"][0]["name"] == "extract"
    assert report["readings"]["dag_nodes"][0]["status"] == DAGNodeStatus.FAILED.value


def test_a_run_that_failed_before_the_outcome_still_leaves_the_identity(
    tmp_path: Path,
) -> None:
    """The runs that most need evidence are the ones where ``run_full_dag``
    itself raised — a stalled DAG, a pod that never served — and there is no
    outcome to describe. The connector, the queue and the seed version are still
    knowable, and after the job is gone this file is where they exist."""
    suite = _suite(tmp_path)

    suite._collect_failure_evidence(RuntimeError("AE never answered"), None)

    report = json.loads(
        (tmp_path / "_Suite" / "report.json").read_text(encoding="utf-8")
    )
    assert report["readings"]["seed_version"] == 1_700_000_099
    assert report["readings"]["connection_qualified_name"] == _CONN_QN
    assert "ae_run_id" not in report["readings"]


def test_the_traceback_ships_as_its_own_artifact(tmp_path: Path) -> None:
    suite = _suite(tmp_path)
    try:
        raise ValueError("boom")
    except ValueError as error:
        suite._collect_failure_evidence(error, None)

    assert "ValueError: boom" in (tmp_path / "_Suite" / "traceback.txt").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Redaction, which is the point
# ---------------------------------------------------------------------------


def test_a_credential_in_a_node_error_does_not_reach_the_artifact(
    tmp_path: Path,
) -> None:
    """A driver's own message is where a connection-string password most often
    surfaces, and the node error is that message quoted verbatim."""
    suite = _suite(tmp_path)

    suite._collect_failure_evidence(AssertionError("nope"), _outcome())

    written = _written(tmp_path)
    assert "hunter2" not in written
    assert PLACEHOLDER in written


def test_the_ambient_api_key_does_not_reach_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value the harness is holding, blanked by literal rather than by key
    name — it reaches a log with no key beside it, which is the shape key
    matching cannot see."""
    monkeypatch.setenv("ATLAN_API_KEY", "atlan-secret-token")
    suite = _suite(tmp_path)

    suite._collect_failure_evidence(
        AssertionError("submit rejected: sent atlan-secret-token"), None
    )

    assert "atlan-secret-token" not in _written(tmp_path)


def test_the_tenant_hostname_does_not_reach_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a credential, but it identifies a customer environment and the bundle
    is uploaded and retained."""
    monkeypatch.setenv("ATLAN_BASE_URL", "https://a-real-tenant.atlan.com")
    suite = _suite(tmp_path)

    suite._collect_failure_evidence(
        AssertionError("GET https://a-real-tenant.atlan.com/api failed"), None
    )

    assert "a-real-tenant" not in _written(tmp_path)


# ---------------------------------------------------------------------------
# It cannot become the failure
# ---------------------------------------------------------------------------


def test_a_collector_failure_does_not_replace_the_verdict(tmp_path: Path) -> None:
    """This runs inside an ``except`` block whose job is to re-raise a real
    failure. A collector that raised would replace the thing being diagnosed —
    the exact miscue ``teardown_method`` is written to avoid."""
    suite = _suite(tmp_path)

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("evidence is broken")

    suite._failure_evidence = _explode  # type: ignore[method-assign]

    suite._collect_failure_evidence(AssertionError("the real failure"), None)


def test_an_empty_evidence_dir_writes_nothing(tmp_path: Path) -> None:
    """The documented opt-out, for a suite that has somewhere else to put it."""
    suite = _suite(tmp_path)
    suite.evidence_dir = ""

    suite._collect_failure_evidence(AssertionError("nope"), _outcome())

    assert list(tmp_path.iterdir()) == []


def test_a_passing_run_writes_nothing(tmp_path: Path) -> None:
    """The wrapper only fires on the way out through an exception, so a green leg
    does not pay for the bundle or ship one nobody asked for."""
    suite = _suite(tmp_path)
    suite.source_available = True
    suite.expect_connection = False
    suite.run_full_dag = lambda: _outcome(failed=False)  # type: ignore[method-assign]
    suite._core_dag_ok = lambda _result: True  # type: ignore[method-assign]

    suite.test_full_dag_runs_end_to_end()

    assert list(tmp_path.iterdir()) == []


def test_the_assertion_failure_is_re_raised_after_collection(tmp_path: Path) -> None:
    """Collect, then re-raise unchanged. Swallowing here would turn a red leg
    green, which is a strictly worse bug than collecting nothing."""
    suite = _suite(tmp_path)
    suite.source_available = True
    suite.run_full_dag = lambda: _outcome()  # type: ignore[method-assign]

    with pytest.raises(AssertionError, match="Full-DAG e2e failed"):
        suite.test_full_dag_runs_end_to_end()

    assert (tmp_path / "_Suite" / "report.json").exists()
