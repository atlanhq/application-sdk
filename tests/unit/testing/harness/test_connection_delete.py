"""Unit tests for teardown through the ``connection-delete`` app (FND-1724).

Two halves, split the way the seed package's tests are split: the DAG and the
submit body are pure and pinned exactly, and the orchestration around them is
checked for the one property teardown actually has to hold — *it never raises,
and it says which failure happened*.

**The node is pinned field for field against the app's own manifest**, not
loosely. ``atlan-connection-delete-app``'s ``app/generated/manifest.json``
declares the workflow type, the queue template and the three args its top-level
input reads; a node that drifts from those runs a workflow the tenant's worker
does not recognise, minutes after the assertions have already passed. Nothing
in a green CI leg would say so — the run's verdict is decided before teardown
starts — which is exactly why these are asserted here rather than trusted.

**The app-absent case gets its own field, and its own test.** A tenant without
``connection-delete`` installed is the failure mode that reads identically to a
clean run: the leg is green, the assets are gone (the runner-side fallback took
them), and the byte-stores accumulate forever. Distinguishing it from a plain
failed run is what lets the caller name the missing queue in its warning instead
of printing a generic cleanup line.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.harness.automation_engine import NoWorkerOnTaskQueueError
from application_sdk.testing.harness.automation_engine.wire import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness.teardown import (
    CONNECTION_DELETE_NODE_ID,
    ConnectionDeletePlan,
    DeleteType,
    build_connection_delete_dag,
    build_connection_delete_submit_payload,
    connection_delete_task_queue,
    delete_connection,
)

_QN = "default/snowflake/1787587123106596"
_QUEUE = "atlan-connection-delete-production"


def _plan(**overrides: Any) -> ConnectionDeletePlan:
    """A plan with everything a real leg supplies, overridable per test."""
    defaults: dict[str, Any] = {
        "connector_short_name": "coalesce",
        "task_queue": _QUEUE,
        "ae_workflow_name": "coalesce-e2e-1787587123-teardown-1",
        "app_service_url": "http://coalesce.svc",
        "run_id": 1787587123,
    }
    return ConnectionDeletePlan(**{**defaults, **overrides})


def _result(
    *statuses: DAGNodeStatus, run: DAGRunStatus = DAGRunStatus.SUCCEEDED
) -> DAGRunResult:
    """A ``native-status`` reading with one node per supplied status."""
    return DAGRunResult(
        run_id="run-1",
        workflow_slug="slug-1",
        status=run,
        nodes=[
            DAGNodeResult(
                name=CONNECTION_DELETE_NODE_ID,
                status=status,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
            for status in statuses
        ],
    )


class _FakeAE:
    """The four AE writes a delete makes, scripted rather than performed."""

    def __init__(
        self,
        *,
        result: DAGRunResult | None = None,
        poll_error: BaseException | None = None,
        submit_error: BaseException | None = None,
        create_error: BaseException | None = None,
    ) -> None:
        self._result = result or _result(DAGNodeStatus.SUCCEEDED)
        self._poll_error = poll_error
        self._submit_error = submit_error
        self._create_error = create_error
        self.created: list[tuple[str, str]] = []
        self.versions: list[dict[str, Any]] = []
        self.submits: list[dict[str, Any]] = []
        self.poll_kwargs: list[dict[str, Any]] = []

    async def create_workflow(self, *, name: str, description: str) -> str:
        if self._create_error is not None:
            raise self._create_error
        self.created.append((name, description))
        return "slug-1"

    async def wait_for_slug(self, slug: str) -> None:
        return None

    async def create_version(self, slug: str, body: dict[str, Any]) -> int:
        self.versions.append(body)
        return int(body["version"])

    async def publish_version(self, slug: str, version: int) -> None:
        return None

    async def submit_workflow(self, payload: dict[str, Any], **kwargs: Any) -> str:
        if self._submit_error is not None:
            raise self._submit_error
        self.submits.append(payload)
        return "run-1"

    async def poll_native_status(self, run_id: str, **kwargs: Any) -> DAGRunResult:
        self.poll_kwargs.append(kwargs)
        if self._poll_error is not None:
            raise self._poll_error
        return self._result


class TestTheNodeMatchesTheAppsManifest:
    """Every field the tenant's worker reads, pinned against what it declares."""

    def test_the_queue_mirrors_the_publish_queue_shape(self) -> None:
        """``atlan-{app}-{deployment}``, resolved from the tenant's deployment
        rather than pinned — one suite runs against several tenants in one CI
        run, and only one of them is called ``production``."""
        assert connection_delete_task_queue("production") == _QUEUE
        assert (
            connection_delete_task_queue("staging") == "atlan-connection-delete-staging"
        )

    def test_the_node_carries_the_workflow_type_the_worker_registers(self) -> None:
        node = build_connection_delete_dag(
            connection_qualified_name=_QN, task_queue=_QUEUE
        )[CONNECTION_DELETE_NODE_ID]
        assert node["inputs"]["workflow_type"] == "connection-delete"
        assert node["app_task_queue"] == _QUEUE
        assert node["inputs"]["task_queue"] == _QUEUE

    def test_the_args_are_the_three_the_apps_input_reads(self) -> None:
        """No more and no fewer. The storage params and search tuning live on
        the app's per-task contracts with their own defaults, and a teardown
        that pinned them would drift the moment the app retuned them."""
        args = build_connection_delete_dag(
            connection_qualified_name=_QN, task_queue=_QUEUE
        )[CONNECTION_DELETE_NODE_ID]["inputs"]["args"]
        assert args == {
            "connection_qualified_name": _QN,
            "delete_type": "PURGE",
            "delete_assets": True,
        }

    def test_purge_is_the_default_rather_than_the_apps_own_soft(self) -> None:
        """The app defaults to SOFT, which leaves every run's assets recoverable
        and still indexed. An ephemeral e2e connection is never coming back, and
        PURGE is what the ``pyatlan`` purge this replaced did."""
        args = build_connection_delete_dag(
            connection_qualified_name=_QN, task_queue=_QUEUE
        )[CONNECTION_DELETE_NODE_ID]["inputs"]["args"]
        assert args["delete_type"] == DeleteType.PURGE.value

    def test_every_argument_is_a_literal(self) -> None:
        """There is no producing node to thread ``$.<node>.outputs.*`` from, and
        a teardown whose target came from a reference would be one the harness
        could not state."""
        node = build_connection_delete_dag(
            connection_qualified_name=_QN, task_queue=_QUEUE
        )[CONNECTION_DELETE_NODE_ID]
        assert not any(
            isinstance(value, str) and value.startswith("$.")
            for value in node["inputs"]["args"].values()
        )

    def test_the_graph_is_one_node(self) -> None:
        """One run per connection is what keeps teardown both *ordered* (the
        referrer before the referent) and *independent* (one stuck delete cannot
        orphan the rest). A multi-node graph has one or the other."""
        dag = build_connection_delete_dag(
            connection_qualified_name=_QN, task_queue=_QUEUE
        )
        assert list(dag) == [CONNECTION_DELETE_NODE_ID]
        assert "depends_on" not in dag[CONNECTION_DELETE_NODE_ID]


class TestTheSubmitBody:
    """The connector's own builder, so AE's submit shape has one definition."""

    def _payload(self) -> dict[str, Any]:
        return build_connection_delete_submit_payload(
            connection_qualified_name=_QN,
            connector_short_name="coalesce",
            display_name="snowflake-seed",
            run_id=1787587123,
            ae_workflow_slug="slug-1",
            app_service_url="http://coalesce.svc",
        )

    def test_it_carries_the_slug_ae_minted(self) -> None:
        assert self._payload()["metadata"]["ae_workflow_slug"] == "slug-1"

    def test_no_credential_block_rides_a_teardown(self) -> None:
        """A delete names a connection, not a source — there is nothing to
        authenticate to, and a credential block would create one to no end."""
        assert not self._payload().get("payload")

    def test_the_credential_token_is_emptied_rather_than_left_unsubstituted(
        self,
    ) -> None:
        """Nothing substitutes ``{{credentialGuid}}`` on a submit with no
        ``payload[]``, and an unsubstituted token is what ``submit_workflow``
        warns about on every teardown it would otherwise fire on."""
        task = self._payload()["spec"]["templates"][0]["dag"]["tasks"][0]
        rows = {p["name"]: p["value"] for p in task["arguments"]["parameters"]}
        assert rows["credential-guid"] == ""


class TestDeleteConnectionReportsRatherThanRaises:
    """Teardown runs post-verdict, so nothing here may become the verdict."""

    async def test_a_succeeded_run_is_complete(self) -> None:
        ae = _FakeAE()
        report = await delete_connection(_QN, ae=ae, plan=_plan())
        assert report.complete
        assert report.succeeded
        assert report.ae_run_id == "run-1"
        assert report.errors == ()

    async def test_the_workflow_description_names_the_connection(self) -> None:
        """The name has to be unique and escaping-free, so the QN goes on the
        description — which is where a reader of an AE run list finds which
        connection a teardown was for."""
        ae = _FakeAE()
        await delete_connection(_QN, ae=ae, plan=_plan())
        _name, description = ae.created[0]
        assert _QN in description

    async def test_no_worker_on_the_queue_is_reported_as_an_absent_app(self) -> None:
        """The one failure with its own field: the remediation is "install the
        app", not "investigate the run"."""
        ae = _FakeAE(
            poll_error=NoWorkerOnTaskQueueError(
                message="nothing polled the queue", resource=_QUEUE
            )
        )
        report = await delete_connection(_QN, ae=ae, plan=_plan())
        assert report.app_absent
        assert not report.complete
        assert _QUEUE in report.errors[0]

    async def test_the_stall_latch_is_armed_with_the_apps_own_queue(self) -> None:
        """Armed, or a tenant without the app burns the full ceiling on every
        connection before the fallback gets to reclaim anything — and named, or
        the operator is told a queue had no worker without being told which."""
        ae = _FakeAE()
        await delete_connection(_QN, ae=ae, plan=_plan(stall_grace_seconds=90))
        assert ae.poll_kwargs[0]["stall_grace_seconds"] == 90
        assert ae.poll_kwargs[0]["stall_task_queue"] == _QUEUE

    async def test_the_progress_watchdog_is_not_armed(self) -> None:
        """``connection-delete`` is a single node that legitimately sits Running
        while it drains a connection, which a node-glyph comparison cannot tell
        from a wedge. The poll ceiling is the bound instead."""
        ae = _FakeAE()
        await delete_connection(_QN, ae=ae, plan=_plan())
        assert ae.poll_kwargs[0]["progress_stall_seconds"] is None

    async def test_a_zero_grace_disables_the_latch_rather_than_firing_at_once(
        self,
    ) -> None:
        ae = _FakeAE()
        await delete_connection(_QN, ae=ae, plan=_plan(stall_grace_seconds=0))
        assert ae.poll_kwargs[0]["stall_grace_seconds"] is None

    async def test_a_failed_node_is_reported_without_raising(self) -> None:
        ae = _FakeAE(
            result=_result(DAGNodeStatus.FAILED, run=DAGRunStatus.FAILED),
        )
        report = await delete_connection(_QN, ae=ae, plan=_plan())
        assert not report.complete
        assert not report.app_absent
        assert "did not succeed" in report.errors[0]

    async def test_a_poll_that_ran_out_of_budget_is_not_read_as_a_verdict(
        self,
    ) -> None:
        """``poll_native_status`` returns its last observation when the ceiling
        expires. Treating those node states as the answer would report a slow
        delete as a failed one — and, worse, a *stopped-watching* run as a
        complete one when the last glyphs happened to be green."""
        timed_out = DAGRunResult(
            run_id="run-1",
            workflow_slug="slug-1",
            status=DAGRunStatus.RUNNING,
            nodes=_result(DAGNodeStatus.SUCCEEDED).nodes,
            timed_out_after_seconds=900.0,
        )
        report = await delete_connection(
            _QN, ae=_FakeAE(result=timed_out), plan=_plan()
        )
        assert not report.succeeded
        assert "had not finished" in report.errors[0]

    async def test_a_rejected_create_is_reported(self) -> None:
        ae = _FakeAE(create_error=RuntimeError("AE rejected the create"))
        report = await delete_connection(_QN, ae=ae, plan=_plan())
        assert not report.complete
        assert "could not publish" in report.errors[0]

    async def test_a_rejected_submit_is_reported(self) -> None:
        ae = _FakeAE(submit_error=RuntimeError("AE rejected the submit"))
        report = await delete_connection(_QN, ae=ae, plan=_plan())
        assert not report.complete
        assert report.ae_workflow_slug == "slug-1"
        assert "could not submit" in report.errors[0]

    async def test_an_empty_qualified_name_deletes_nothing(self) -> None:
        """Blank is never a legitimate target: the app's own guard refuses it
        because its prefix deletes would otherwise degrade to tenant-wide roots.
        Refusing here means the submit that would be refused is never made."""
        ae = _FakeAE()
        report = await delete_connection("", ae=ae, plan=_plan())
        assert not report.complete
        assert ae.created == []
        assert ae.submits == []


@pytest.mark.parametrize(
    "delete_type", [DeleteType.SOFT, DeleteType.HARD, DeleteType.PURGE]
)
def test_every_delete_type_is_one_the_app_accepts(delete_type: DeleteType) -> None:
    """The app validates the string itself and fails the run on anything else,
    minutes after the verdict is in — so the enum is the guard at the call
    site."""
    args = build_connection_delete_dag(
        connection_qualified_name=_QN, task_queue=_QUEUE, delete_type=delete_type
    )[CONNECTION_DELETE_NODE_ID]["inputs"]["args"]
    assert args["delete_type"] in {"SOFT", "HARD", "PURGE"}
