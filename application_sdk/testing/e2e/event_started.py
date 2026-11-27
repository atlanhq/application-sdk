"""Full-DAG e2e base for runs an event starts (openlineage, anomalo, auth, …).

An event-driven entrypoint is never submitted: Automation Engine starts a run of
it when a matching event arrives on the topic its contract's ``events {}`` block
names. :class:`BaseE2ETest` and :class:`SystemAppE2ETest` both publish a seed DAG
and submit it, which runs the entrypoint's code but skips everything the
contract declares about *when* it runs: the topic, the filter rules, the
streaming or batch dispatch, the ack paths. A wrong filter is invisible to a
submitted run.

:class:`EventStartedE2ETest` does the upstream action instead and adopts the run
it causes:

1. It resolves the trigger workflow Local Marketplace reconciled from the app's
   manifest on install. Nothing is seeded or published, so the DAG that runs is
   the tenant's own, with every node on the queue the manifest names.
2. :meth:`EventStartedE2ETest.send_event` does the real action: a post to the
   tenant's events gateway, a change in Atlas, or a run on a source that emits
   the event.
3. It finds the run AE created under that workflow since the send, with
   :meth:`~application_sdk.testing.harness.automation_engine.AEClient.find_run_created_since`,
   and hands the run id to the base class, which polls, reads Atlas and grades
   exactly as it does for a submitted run.

What a suite declares:

* ``connector_short_name``: the app's name, as Local Marketplace knows it.
* ``trigger_entrypoint``: the entrypoint whose trigger the event fires. The two
  together name the trigger workflow; see :func:`trigger_workflow_slug`.
* :meth:`~EventStartedE2ETest.send_event`: the upstream action.
* ``required_dag_nodes``: the nodes of the entrypoint's own DAG that must
  succeed. The default names a connector's ``extract`` / ``publish``.

What it cannot do:

* **Object-store expectations.** ``sdk:store-assert`` is a node the harness
  appends to a DAG it publishes. This path publishes nothing, so a suite that
  needs store claims runs a second, submitted suite for them.
* **Tell two events' runs apart on a shared topic.** The trigger workflow is the
  tenant's, not this run's, so "a run under this slug since the send" is this
  suite's run only when nothing else fires the trigger meanwhile. On a dedicated
  topic under the tenant lease that holds. On ``ATLAS_ENTITIES`` it does not:
  the suite's own seed writes fire the same trigger. There the adopted run
  proves the trigger fires and the DAG succeeds, and the assertion that the
  *suite's* event was handled is the Atlas effect on run-scoped assets
  (``expected_asset_attributes_at``, :meth:`BaseE2ETest.assert_dag_outcome`).

The run is never terminated in teardown, for the same reason: it may be carrying
events that are not this suite's.

Tenant pool: this class does not pick one. On its own it runs on the connector
pool, like :class:`BaseE2ETest`; a system app inherits
:class:`~application_sdk.testing.e2e.system_app.SystemAppE2ETest` as well, after
this class, and runs on the system pool::

    class TestEventSync(EventStartedE2ETest, SystemAppE2ETest):
        connector_short_name = "example"
        trigger_entrypoint = "event-sync"
        required_dag_nodes = ("extract",)

        async def send_event(self) -> None:
            await self.post_event("example", {"id": self.run_id})
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    EventGatewayRejectedError,
    EventRunNotStartedError,
    EventTriggerNotInstalledError,
    HarnessMethodNotImplementedError,
)
from application_sdk.testing.e2e.base import BaseE2ETest

logger = get_logger(__name__)

__all__ = ["EventStartedE2ETest", "trigger_workflow_slug"]

_SLUG_MAX_LENGTH = 63
_GATEWAY_TIMEOUT_SECONDS = 30.0


def trigger_workflow_slug(app_name: str, entrypoint: str) -> str:
    """The AE workflow slug Local Marketplace gives an app entrypoint's triggers.

    Local Marketplace reconciles ``triggers.schedules`` / ``triggers.events``
    from an entrypoint's manifest into one AE workflow, whose slug it derives
    from the app and entrypoint names (``workflow_slug`` in its
    ``schedule_reconciler``). This mirrors that derivation, including the
    RFC 1123 clean-up, so a suite can name the workflow without reading it back.
    The rule is a cross-repo contract; see
    ``docs/standards/cross-repo-contracts.md``.

    Args:
        app_name: The app's name, as its catalog entry carries it.
        entrypoint: The entrypoint's name.

    Returns:
        The slug, for example ``openlineage-generic-lineage``.
    """
    name = f"{app_name}-{entrypoint}".lower().replace(" ", "-").replace("_", "-")
    name = re.sub(r"[^a-z0-9\-.]", "", name)
    name = re.sub(r"[-.]+", "-", name).strip("-.")
    if len(name) > _SLUG_MAX_LENGTH:
        name = name[:_SLUG_MAX_LENGTH].rstrip("-.")
    return name or "default"


class EventStartedE2ETest(BaseE2ETest):
    """Pytest base for a suite whose run a real event starts."""

    _required_class_attrs: ClassVar[tuple[str, ...]] = (
        "connector_short_name",
        "trigger_entrypoint",
    )

    #: The entrypoint whose event trigger :meth:`send_event` fires.
    trigger_entrypoint: ClassVar[str] = ""

    #: How long AE has to create the run once the event is sent. The default
    #: covers the batch path, where AE starts a run at most once every ten
    #: minutes; a streaming trigger answers in seconds.
    event_run_timeout_seconds: ClassVar[int] = 900
    event_run_poll_seconds: ClassVar[int] = 15

    # Nothing is seeded on this path, so there is no seed to compare the
    # deployed manifest against; see _assert_deployed_manifest_matches below.
    assert_deployed_manifest: ClassVar[bool] = False
    # An event-driven entrypoint acts on assets it is told about. A suite whose
    # DAG does land a Connection (or lineage) says so.
    expect_connection: ClassVar[bool] = False
    expect_lineage: ClassVar[bool] = False
    # The no-worker watchdog watches the CI worker's queue, which no node of the
    # tenant's own DAG is dispatched to.
    ae_stall_grace_seconds: ClassVar[int] = 0

    # ------------------------------------------------------------------
    # What a suite overrides
    # ------------------------------------------------------------------

    async def send_event(self) -> None:
        """Do the upstream action that makes the tenant emit the event.

        Called once per run, after :meth:`BaseE2ETest.seed_prerequisites` and
        after the trigger workflow has been found. It should return once the
        action is accepted, not once its effect is visible: the harness does the
        waiting.

        Raises:
            HarnessMethodNotImplementedError: The suite did not override it.
        """
        raise HarnessMethodNotImplementedError(
            message=f"{type(self).__name__} must override send_event()",
        )

    def trigger_workflow_slug(self) -> str:
        """The AE workflow the event's run appears under.

        ``ae_workflow_slug`` when the suite pins one, else the slug Local
        Marketplace derives for ``connector_short_name`` and
        ``trigger_entrypoint``.
        """
        return self.ae_workflow_slug or trigger_workflow_slug(
            self.connector_short_name, self.trigger_entrypoint
        )

    # ------------------------------------------------------------------
    # Events gateway
    # ------------------------------------------------------------------

    def _event_gateway_transport(self) -> httpx.AsyncBaseTransport | None:
        """Transport for :meth:`post_event`. ``None`` is httpx's default."""
        return None

    async def post_event(
        self,
        path: str,
        body: Mapping[str, object] | Sequence[object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """POST *body* as JSON to the tenant's events gateway.

        Args:
            path: The route below ``/events/``, for example
                ``openlineage/airflow/api/v1/lineage``.
            body: The event, as the upstream system would send it.
            headers: Extra request headers, for a route that authenticates with
                something other than the run's API key. They win over the
                default ``Authorization`` header.

        Raises:
            EventGatewayRejectedError: The gateway did not accept the event.
        """
        auth = self._read_tenant_auth()
        url = f"{auth.base_url}/events/{path.lstrip('/')}"
        request_headers = {"Authorization": f"Bearer {auth.api_key}", **(headers or {})}
        try:
            async with httpx.AsyncClient(
                timeout=_GATEWAY_TIMEOUT_SECONDS,
                transport=self._event_gateway_transport(),
            ) as client:
                response = await client.post(url, json=body, headers=request_headers)
        except httpx.HTTPError as exc:
            raise EventGatewayRejectedError(
                message=(
                    f"POST /events/{path.lstrip('/')} did not reach the tenant's "
                    f"events gateway: {type(exc).__name__}: {exc}"
                ),
            ) from exc
        if response.status_code >= 300:
            raise EventGatewayRejectedError(
                message=(
                    f"POST /events/{path.lstrip('/')} answered HTTP "
                    f"{response.status_code}, so the event was not accepted and "
                    "no run can follow from it. Not a verdict on the app."
                ),
                status_code=response.status_code,
            )

    # ------------------------------------------------------------------
    # The three seams in BaseE2ETest._run_full_dag_async
    # ------------------------------------------------------------------

    async def _bootstrap_workflow(self) -> str:
        """Resolve the tenant's trigger workflow; seed and publish nothing.

        Returns:
            The trigger workflow's slug.

        Raises:
            EventTriggerNotInstalledError: AE has no workflow under the slug, so
                no event can start a run of it.
        """
        slug = self.trigger_workflow_slug()
        resolves = await self._ae.slug_resolves(slug)
        if resolves is False:
            raise EventTriggerNotInstalledError(
                message=(
                    f"AE has no workflow '{slug}', the one Local Marketplace "
                    f"creates for the triggers of {self.connector_short_name}'s "
                    f"'{self.trigger_entrypoint}' entrypoint. Either the app is "
                    "not installed on this tenant, or its install did not "
                    "reconcile triggers (the entrypoint must be listed in "
                    "atlan.yaml and its manifest must carry triggers.events). "
                    "No event can start a run until it exists."
                ),
                resource=slug,
            )
        if resolves is None:
            logger.warning(
                "Could not confirm AE workflow %s exists; sending the event "
                "anyway and letting the run lookup decide",
                slug,
            )
        published = await self._ae.get_published_version(slug)
        if published is not None and published.dag:
            # Diagnostics only: lets a stuck node name the queue it waits on.
            self._capture_node_dispatch(published.dag)
        return slug

    async def _submit(self, payload: dict[str, Any], *, slug: str) -> str:
        """Send the event, then adopt the run AE starts for it.

        *payload* is the Heracles envelope :class:`BaseE2ETest` builds for every
        run; nothing is submitted on this path, so it is unused.

        Returns:
            The id of the newest run under *slug* created since the send.

        Raises:
            EventRunNotStartedError: No such run appeared in the budget.
        """
        del payload
        since = datetime.now(timezone.utc)
        logger.info(
            "Sending the event for %s; then waiting up to %ds for a run of %s",
            type(self).__name__,
            self.event_run_timeout_seconds,
            slug,
        )
        await self.send_event()
        lookup = await self._ae.find_run_created_since(
            slug,
            since,
            timeout_seconds=self.event_run_timeout_seconds,
            interval_seconds=self.event_run_poll_seconds,
        )
        if lookup.run_id is not None:
            return lookup.run_id
        if lookup.conclusive:
            detail = (
                f"AE answered and lists no run of '{slug}' created since "
                f"{since.isoformat()}. The event was sent but did not start a "
                "run: check that the trigger is ACTIVE, that the event matches "
                "the contract's filter rules, and that whatever writes the "
                "topic (the events gateway, Atlas, CDC) is running on this "
                "tenant."
            )
        else:
            detail = (
                f"AE's run list for '{slug}' could not be read, so whether the "
                "event started a run is unknown. Not a verdict on the app."
            )
        raise EventRunNotStartedError(
            message=(
                f"No run followed the event within "
                f"{self.event_run_timeout_seconds}s. {detail}"
            ),
            resource=slug,
        )

    def _track_unsettled_run(self, run_id: str) -> None:
        """No-op: the run belongs to the tenant's trigger workflow.

        It may be carrying events other than this suite's, so teardown must not
        terminate it. The poll ceiling still bounds how long the suite waits.
        """
        del run_id

    async def _assert_deployed_manifest_matches(self, slug: str) -> None:
        """No-op: the executed DAG is the one the tenant already published."""
        logger.info(
            "Deployed-manifest check not applicable for %s: the run under %s "
            "executes the tenant's own published DAG",
            type(self).__name__,
            slug,
        )
