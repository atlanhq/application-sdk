"""Full-DAG e2e base for system apps (connection-delete, popularity, publish, …).

A system app runs only inside a tenant, never in SDR mode, and its production
DAGs are usually declared by the connectors that call it rather than served by
the app itself. :class:`BaseE2ETest` cannot run such a suite as-is: it submits
through Heracles, which re-derives the graph from the named app's served
manifest — an app that serves none fails that submit outright, and one that does
gets its manifest republished over the harness's DAG.

:class:`SystemAppE2ETest` submits straight to AE instead
(:meth:`~application_sdk.testing.harness.automation_engine.AEClient.submit_published_version`):
AE starts the version the harness just published, with no manifest fetch, so
the graph that runs is exactly the one the suite seeded. Everything else — the
seed DAG from ``manifest_path``, node routing, polling, grading and teardown —
is :class:`BaseE2ETest`'s.

What a suite declares:

* ``connector_short_name`` — the app's name; the only required attribute.
  ``argo_package_name`` / ``argo_template_name`` name a Heracles envelope this
  path never sends, so they are optional here.
* ``manifest_path`` — a JSON file whose top-level ``dag`` is the graph to run:
  the app's own generated manifest when it has one, else a fixture copied from
  the connector manifest that declares the node.
* ``required_dag_nodes`` — the nodes that must succeed. The default names a
  connector's ``extract`` / ``publish`` and will not match a system app's DAG.
* Run-scoped arguments go through :meth:`BaseE2ETest._mustache_substitutions`
  exactly as for a connector: the seed DAG's exact-match ``{{...}}`` strings are
  replaced from the model it returns. Subclass the substitutions model with an
  aliased field per placeholder and return an instance from the override.
  Nothing else substitutes on this path, so every placeholder the DAG carries
  needs a field.

Node routing is unchanged: only a node named ``extract`` is pointed at the CI
worker, and every other node keeps its manifest task queue with
``{deployment_name}`` resolved to the tenant's — which, with the app installed
on the tenant, is where a system app's node runs.

A suite built on this class runs only on the system-app tenant pool; see
:mod:`application_sdk.testing.e2e.tenant_pool`.

Object-store expectations (FND-3571). The runner cannot see the tenant's object
store, so a suite that needs to prove what the app left there returns its claims
from :meth:`SystemAppE2ETest.store_expectations`. The harness then appends one
``sdk:store-assert`` node after every other node, on the app's own task queue —
the same pod and store binding the app used — and grades the verdict that node
returns. Every SDK worker serves that workflow; it only LISTs, only under
``artifacts/apps/``, ``persistent-artifacts/`` and ``connection-cache/``, and
returns counts::

    class TestPurge(SystemAppE2ETest):
        def store_expectations(self):
            return [
                StoreExpectation(
                    prefix=f"persistent-artifacts/{self.connection_qualified_name}",
                    kind=StoreExpectationKind.ABSENT,
                ),
            ]
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, ClassVar

from application_sdk.errors.base import AppError
from application_sdk.execution._temporal.store_assert import (
    STORE_ASSERT_WORKFLOW_TYPE,
    StoreAssertInput,
    StoreAssertOutput,
    StoreExpectation,
    StoreExpectationKind,
    StoreObservation,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    StoreAssertQueueAmbiguousError,
    StoreAssertUnreadableError,
)
from application_sdk.testing.e2e.base import BaseE2ETest, FullDAGOutcome
from application_sdk.testing.e2e.tenant_pool import TenantPool
from application_sdk.testing.harness.automation_engine.wire import DAGRunResult
from application_sdk.testing.harness.expectations import Unreadable

logger = get_logger(__name__)

__all__ = [
    "STORE_ASSERT_NODE_ID",
    "StoreExpectation",
    "StoreExpectationKind",
    "SystemAppE2ETest",
]

STORE_ASSERT_NODE_ID = "sdk-store-assert"
"""DAG node id of the appended assertion node."""


class SystemAppE2ETest(BaseE2ETest):
    """Pytest base for a system app's full-DAG suite, submitted straight to AE."""

    _required_class_attrs: ClassVar[tuple[str, ...]] = ("connector_short_name",)
    _tenant_pool: ClassVar[TenantPool] = TenantPool.SYSTEM

    # Nothing republishes over the seed on this path, so there is no deployed
    # manifest to compare; see _assert_deployed_manifest_matches below.
    assert_deployed_manifest: ClassVar[bool] = False

    # A system app acts on connections other apps own. A suite whose DAG does
    # land a Connection (or lineage) says so, as a connector suite would.
    expect_connection: ClassVar[bool] = False
    expect_lineage: ClassVar[bool] = False

    async def _submit(self, payload: dict[str, Any], *, slug: str) -> str:
        """Start the version :meth:`_bootstrap_workflow` published, via AE.

        *payload* is the Heracles envelope :class:`BaseE2ETest` builds for every
        run; this path sends no envelope, so it is unused.
        """
        del payload
        return await self._ae.submit_published_version(slug)

    async def _assert_deployed_manifest_matches(self, slug: str) -> None:
        """No-op: AE ran the published seed, and nothing republished over it."""
        logger.info(
            "Deployed-manifest check not applicable for %s: submitted straight "
            "to AE against slug %s, so the executed DAG is the seeded one",
            type(self).__name__,
            slug,
        )

    # ------------------------------------------------------------------
    # Object-store expectations (FND-3571)
    # ------------------------------------------------------------------

    def store_expectations(self) -> Sequence[StoreExpectation]:
        """What the object store must hold once the app's nodes have finished.

        Override to assert on the store. Called while a DAG run is active, so a
        multi-run suite can branch on ``self._dag.label``. Empty (the default)
        appends no node and grades nothing.

        Returns:
            At most
            :data:`~application_sdk.execution._temporal.store_assert.MAX_STORE_EXPECTATIONS`
            claims; each prefix must sit strictly below ``artifacts/apps/``,
            ``persistent-artifacts/`` or ``connection-cache/``.
        """
        return ()

    def store_assert_task_queue(self, seed_dag: dict[str, Any]) -> str:
        """The task queue the assertion node is dispatched to.

        The app under test's own queue: the one queue every non-``extract``
        node in the seed DAG names. Override when the DAG spans several.

        Raises:
            StoreAssertQueueAmbiguousError: The DAG names zero or several.
        """
        queues = sorted(
            {
                node["inputs"]["task_queue"]
                for name, node in seed_dag.items()
                if name != "extract"
                and isinstance(node, dict)
                and isinstance(node.get("inputs"), dict)
                and isinstance(node["inputs"].get("task_queue"), str)
                and node["inputs"]["task_queue"]
            }
        )
        if len(queues) != 1:
            raise StoreAssertQueueAmbiguousError(
                message=(
                    f"{type(self).__name__} declares store expectations, but the "
                    f"seed DAG names {len(queues)} task queue(s) {queues}; "
                    "override store_assert_task_queue() to name the app's own."
                ),
            )
        return queues[0]

    def _build_seed_dag(self) -> dict[str, Any]:
        """The base seed DAG, plus the assertion node when expectations exist."""
        seed_dag = super()._build_seed_dag()
        expectations = list(self.store_expectations())
        if not expectations:
            return seed_dag
        if STORE_ASSERT_NODE_ID in seed_dag:
            raise StoreAssertQueueAmbiguousError(
                message=(
                    f"The seed DAG already has a node named {STORE_ASSERT_NODE_ID!r}; "
                    "the harness cannot append its assertion node."
                ),
                field="manifest_path",
            )
        # Validated here, on the runner, so a malformed claim (too many, a
        # negative count) fails the suite before anything is submitted.
        args = StoreAssertInput(expectations=expectations).model_dump(
            mode="json", include={"expectations"}
        )
        queue = self.store_assert_task_queue(seed_dag)
        app_name = next(
            (
                node["app_name"]
                for node in seed_dag.values()
                if isinstance(node, dict)
                and isinstance(node.get("inputs"), dict)
                and node["inputs"].get("task_queue") == queue
                and isinstance(node.get("app_name"), str)
            ),
            self.connector_short_name,
        )
        upstream = [{"node_id": name, "tag": "success"} for name in sorted(seed_dag)]
        seed_dag[STORE_ASSERT_NODE_ID] = {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "SDK object-store assertions (e2e)",
            "app_name": app_name,
            "app_task_queue": queue,
            "inputs": {
                "workflow_type": STORE_ASSERT_WORKFLOW_TYPE,
                "task_queue": queue,
                "args": args,
            },
            "depends_on": (
                upstream[0] if len(upstream) == 1 else {"and_conditions": upstream}
            ),
        }
        # Re-read so a never-dispatched assertion node names its queue too.
        self._capture_node_dispatch(seed_dag)
        logger.info(
            "Appended %s on %s with %d store expectation(s)",
            STORE_ASSERT_NODE_ID,
            queue,
            len(expectations),
        )
        return seed_dag

    async def _read_atlas(self, ae_result: DAGRunResult) -> FullDAGOutcome:
        """Base Atlas reads, plus the assertion node's verdict when it ran."""
        outcome = await super()._read_atlas(ae_result)
        node = next(
            (n for n in ae_result.nodes if n.name == STORE_ASSERT_NODE_ID), None
        )
        if node is None or not node.status.is_success:
            # Absent: no expectations. Not succeeded: the DAG gate reports it.
            return outcome
        read: StoreAssertOutput | Unreadable
        try:
            outputs = await self._ae.get_node_outputs(
                ae_result.run_id, STORE_ASSERT_NODE_ID
            )
            read = StoreAssertOutput.model_validate(outputs)
        # conformance: ignore[E004] carried on the outcome as Unreadable and raised by the grader; reading it as a pass is the one thing this must not do
        except (AppError, ValueError) as exc:
            logger.warning(
                "Could not read the %s verdict for run %s",
                STORE_ASSERT_NODE_ID,
                ae_result.run_id,
                exc_info=True,
            )
            read = Unreadable(cause=exc)
        return dataclasses.replace(outcome, store_assert_read=read)

    def _assert_full_dag_outcome(self, outcome: FullDAGOutcome) -> None:
        """The base ladder, then the object-store verdict."""
        super()._assert_full_dag_outcome(outcome)
        read = outcome.store_assert_read
        if read is None:
            if self.store_expectations():
                raise StoreAssertUnreadableError(
                    message=(
                        f"{type(self).__name__} declares store expectations but "
                        f"run {outcome.ae_result.run_id} carries no "
                        f"{STORE_ASSERT_NODE_ID} verdict."
                    ),
                )
            return
        if isinstance(read, Unreadable):
            raise StoreAssertUnreadableError(
                message=(
                    f"The {STORE_ASSERT_NODE_ID} node ran but its verdict could "
                    f"not be read from AE run {outcome.ae_result.run_id}, so the "
                    "store expectations went ungraded. Not a verdict on the app: "
                    f"{type(read.cause).__name__}: {read.cause}"
                ),
            )
        if not read.passed:
            raise AssertionError(
                "Object-store expectations not met (observed in-tenant by "
                f"{STORE_ASSERT_NODE_ID}, run {outcome.ae_result.run_id}):\n"
                + "\n".join(_render_observation(o) for o in read.observations)
            )


def _render_observation(observation: StoreObservation) -> str:
    """One line per prefix: the claim, what was seen, and the verdict."""
    mark = "ok  " if observation.passed else "FAIL"
    claim = observation.kind.value.upper()
    if observation.kind is StoreExpectationKind.COUNT:
        claim += f" == {observation.expected_count}"
    if observation.problem:
        seen = observation.problem
    else:
        seen = f"objects (incl. markers)={observation.objects_all}"
        if observation.objects_data is not None:
            seen += f", data objects={observation.objects_data}"
        if observation.truncated:
            seen += ", truncated"
    return f"  [{mark}] {observation.prefix}: expected {claim}; saw {seen}"
