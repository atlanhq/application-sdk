"""Reclaiming what a harness run created — through the app that owns it.

``seed_assets`` seeds *through publish*, because publish owns the entities and
the cache. Teardown did not follow the same rule: it hand-rolled a ``pyatlan``
``purge_connection`` plus an obstore ``delete_prefix``, and both halves were
wrong in the same way — the harness doing an app's job from outside the tenant.
Symmetry is the point of FND-1724: seeding goes through the app that owns the
artifact, and so does teardown.

**What the old teardown left behind**, per connection, on a shared e2e tenant:

===================================================  ==========  ==============
Artifact                                             Owner       Old teardown
===================================================  ==========  ==============
Connection + entities in Atlas                       harness     purged
``persistent-artifacts/apps/atlan-publish-app/       publish     nothing tried
state/<cqn>/`` — publish-cache-v2, WAL, drift
``connection-cache/<cqn>.sqlite``                    publish     unreachable
Seed NDJSON under ``artifacts/apps/<app>/e2e-seed/``  harness     403
===================================================  ==========  ==============

The 403 and the "unreachable" are the same mechanism, and it is not a
permissions oversight — :mod:`._dag` has the detail. Nothing about it is fixable
from a runner, which is why the node is the fix.

Two functions, and which one runs is not a preference:

:func:`delete_connection`
    The owner. Submits a one-node ``connection-delete`` DAG through AE, exactly
    as :func:`~application_sdk.testing.harness.seed.seed_assets` submits its
    publish node — same client, same shape, same wait for a verdict. It runs on
    the tenant, so it clears the byte-stores as well as Atlas.
:func:`purge_connection`
    The degraded path, in :mod:`._purge`. Reclaims the Atlas half from the
    runner and nothing else — what the harness did for every run before
    FND-1724.

**Why a fallback at all**, given the app is installed on the e2e tenants
(FND-1724, 2026-09-07). Two silences it does not close:

* **The worker-up-only tier wires no AE client** (``source_available=false``).
  There is nothing to submit a delete through, and a connection that tier minted
  still has to go.
* **A graph that is not ours.** Since FND-1775 the submit goes straight to AE
  and fetches no manifest, so the version published moments earlier is the
  version that runs and nothing can be published over it (:mod:`._dag`'s
  historical note has what this replaced). The read-back stays anyway, on the
  same rule as the seed's: if what AE runs is *not* this teardown's node the
  delete reports :attr:`ConnectionDeleteReport.dag_superseded` and the fallback
  runs, so an impossible outcome degrades instead of quietly deleting nothing.
* **A scaled-to-zero worker that does not wake** looks exactly like an absent app
  from here. ``connection-delete``'s ``atlan.yaml`` sets
  ``keda.minReplicaCount: 0``, so its queue is *legitimately* unpolled between
  runs and "nothing started within the grace" is at least as often a cold start
  as a missing install.

It is still transitional — leaking bytes is better than leaking whole
connections, but it is the harness doing an app's job, which is what this whole
change exists to stop. Drop it once the AE-less tier is gone and cold-start wake
is boring.

**Nothing here raises.** Teardown runs after the assertions have decided the
verdict; an exception from cleanup replaces a real failure with a cleanup error
and loses the diagnosis. Both functions return a report instead — a stronger
guarantee than "we remembered to wrap the call". That also settles what a
missing app does: it is reported, loudly and by name, and it never reds a leg.
Tenant cleanliness is not the thing under test.

**What is deliberately not here** is the policy: which connection to delete, and
whether to delete at all. Both functions take a qualified name a run minted for
itself. Pointed at a long-lived shared connection they would delete assets that
are not the run's to delete, and no guard here can tell the two apart — which is
why :mod:`application_sdk.testing.harness.identity` mints the ephemeral name in
the first place, and why a test can predict it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from application_sdk.errors.base import sanitize_cause_repr
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.automation_engine import (
    AEClient,
    NoWorkerOnTaskQueueError,
)
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.starters import AEWorkflowSpec, SubmitRetry
from application_sdk.testing.harness.starters import (
    publish_seed_version as _publish_seed_version,
)
from application_sdk.testing.harness.teardown._dag import (
    CONNECTION_DELETE_APP_NAME,
    CONNECTION_DELETE_NODE_ID,
    CONNECTION_DELETE_WORKFLOW_TYPE,
    DeleteType,
    build_connection_delete_dag,
    connection_delete_task_queue,
)
from application_sdk.testing.harness.teardown._purge import (
    PURGE_BATCH_SIZE,
    PurgeReport,
    purge_connection,
)

logger = get_logger(__name__)

__all__ = [
    "CONNECTION_DELETE_APP_NAME",
    "CONNECTION_DELETE_NODE_ID",
    "CONNECTION_DELETE_WORKFLOW_TYPE",
    "PURGE_BATCH_SIZE",
    "ConnectionDeletePlan",
    "ConnectionDeleteReport",
    "DeleteType",
    "PurgeReport",
    "build_connection_delete_dag",
    "connection_delete_task_queue",
    "delete_connection",
    "purge_connection",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionDeletePlan:
    """How one connection's delete run is addressed, dispatched and waited on.

    The run-scoped wiring, kept apart from the DAG builders for the reason
    :class:`~application_sdk.testing.harness.seed.SeedPublishPlan` is kept apart
    from :class:`~application_sdk.testing.harness.seed.SeedSpec`: the builders
    are pure and a unit test can pin them without a tenant, while this is the
    leg's addressing.

    Attributes:
        connector_short_name: The suite under test — names the AE workflow and
            its labels, so a teardown run is attributable to a leg.
        task_queue: The tenant's connection-delete queue, from
            :func:`connection_delete_task_queue`.
        ae_workflow_name: Name for the AE workflow this delete runs under. Must
            not collide with the suite's own or with another teardown in the
            same run: ``create_workflow`` is idempotent on the name, so a shared
            name would publish one graph over the other.
        run_id: Ignored.

            .. deprecated:: 3.34.0
               It was the submit envelope's run label. The teardown no longer
               builds an envelope, so nothing reads this. Removed in v4.0. See
               FND-1775 and :mod:`._dag`'s historical note.
        delete_type: How thoroughly to delete. ``PURGE`` for e2e teardown — it
            is what the ``pyatlan`` purge this replaced did, and an ephemeral
            connection is never coming back.
        submit_retry: How long to let AE keep answering 404 while the
            just-published version replicates, or ``None`` to leave
            ``submit_published_version``'s own budget in place. It is *not* a
            cold-start budget any more: since FND-1775 this submit calls no app
            pod, and whether the delete app's worker is up is asked by
            :attr:`stall_grace_seconds` instead.
        poll_interval_seconds: Gap between ``native-status`` reads.
        poll_timeout_seconds: Ceiling on the whole delete wait.
        stall_grace_seconds: How long to wait for *any* node to start before
            concluding that nothing polls :attr:`task_queue`. The reason it is a
            distinct (short) budget: when nothing does — a worker that will not
            wake from ``minReplicaCount: 0``, or an app that is not installed —
            every connection would otherwise burn the full
            :attr:`poll_timeout_seconds` before the fallback runs. It has to
            clear a cold start, which is what stops it being shorter still.
            ``0`` disables the latch.
        minter: Supplies the AE seed version. ``None`` mints from the real clock.
    """

    connector_short_name: str
    task_queue: str
    ae_workflow_name: str
    run_id: int
    delete_type: DeleteType = DeleteType.PURGE
    submit_retry: SubmitRetry | None = None
    poll_interval_seconds: int = 10
    poll_timeout_seconds: int = 900
    stall_grace_seconds: int = 120
    minter: Minter | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionDeleteReport:
    """What one ``connection-delete`` run managed to do, and what it did not.

    A report rather than an exception, on the same rule as
    :class:`PurgeReport`: see the module docstring.

    Attributes:
        qualified_name: The connection this run targeted.
        succeeded: Every node of the delete run reached success. The only value
            on which a caller may skip the fallback.
        app_absent: Nothing picked the node up off :attr:`ConnectionDeletePlan.task_queue`
            within the stall grace — the app is not installed on this tenant, or
            its scale-to-zero worker did not wake. Distinguished from a plain
            failure because the remediation is completely different (look at the
            tenant vs. look at the run), and because it is the failure mode that
            otherwise reads exactly like a passing run.
        dag_superseded: The graph AE ran is not the node this teardown
            published — something republished over the version it submitted.
            Since FND-1775 that should be impossible, which is the point of the
            field rather than an argument against it: it asserts the
            AE-native submit's determinism on a live tenant. Its own field for
            the same reason :attr:`app_absent` has one, and a sharper one: what
            ran is another app's DAG, so it can report success. Read as a plain
            failure it would mean skipping the fallback and telling an operator
            a connection was deleted that is still there.
        ae_workflow_slug: Slug of the AE workflow the delete ran under.
        ae_run_id: That run's id — the one link that shows what the app did.
        errors: One line per failed step, in order. Already secret-redacted: an
            AE or pyatlan error can quote the request URL, and a report that
            ships with an evidence bundle is not the place to find that out.
    """

    qualified_name: str
    succeeded: bool = False
    app_absent: bool = False
    dag_superseded: bool = False
    ae_workflow_slug: str = ""
    ae_run_id: str = ""
    errors: Sequence[str] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """Did the delete finish with nothing failing?

        Returns:
            ``True`` when the run succeeded and no step errored — the one
            outcome on which the caller needs no fallback and no warning.
        """
        return self.succeeded and not self.errors


async def delete_connection(
    qualified_name: str,
    *,
    ae: AEClient,
    plan: ConnectionDeletePlan,
) -> ConnectionDeleteReport:
    """Delete one connection, its assets and its byte-stores, via the app.

    Five steps, mirroring :func:`~application_sdk.testing.harness.seed.seed_assets`
    step for step, minus the ones that only a seed needs (there is nothing to
    serialise, validate or upload):

    1. **Create and seed** an AE workflow carrying the one-node DAG.
    2. **Submit** it straight to AE — no envelope, no manifest fetch, so no
       app under test in the path and nothing that can publish over it.
    3. **Check that the graph AE will run is ours** — see
       :meth:`~application_sdk.testing.harness.automation_engine.AEClient.foreign_published_dag`
       and the invariant :mod:`._dag` states. Step 2 is what makes this
       unreachable, and this is the step that does not take that on trust; it
       runs before the poll because a graph that is not ours costs minutes and
       deletes nothing.
    4. **Wait for a verdict**, with the start-grace latch armed so a tenant
       without the app is detected in :attr:`ConnectionDeletePlan.stall_grace_seconds`
       rather than in the full poll ceiling. The run's own node names are checked
       again here, where they cannot be read too early.
    5. **Report.** Never raise — see the module docstring.

    The progress watchdog is deliberately *not* armed. ``connection-delete``
    runs as a single node that legitimately sits ``Running`` for as long as the
    connection takes to drain (its own ``search_and_delete_assets`` loop is
    budgeted in tens of thousands of assets per call), and a watchdog that
    compares node glyphs cannot tell that apart from a wedge. The poll ceiling
    is the bound here.

    Args:
        qualified_name: The connection to delete. Must be a name the run minted
            for itself — see the module docstring on why no guard here can check
            that.
        ae: An open AE client on *this* event loop. Not closed here — the
            transport's lifetime stays with whoever opened it.
        plan: How this delete is addressed, dispatched and waited on.

    Returns:
        The report. Callers decide from :attr:`ConnectionDeleteReport.complete`
        whether to fall back to :func:`purge_connection`.
    """
    if not qualified_name:
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            errors=("no connection qualified name to delete",),
        )

    try:
        seeded = await _publish_seed_version(
            AEWorkflowSpec(
                name=plan.ae_workflow_name,
                description=(
                    "Full-DAG e2e harness — teardown for "
                    f"{plan.connector_short_name}: {qualified_name}"
                ),
                seed_dag=build_connection_delete_dag(
                    connection_qualified_name=qualified_name,
                    task_queue=plan.task_queue,
                    delete_type=plan.delete_type,
                ),
            ),
            client=ae,
            minter=plan.minter,
        )
    # conformance: ignore[E004] teardown boundary — this runs after the assertions have decided the verdict, so a cleanup failure is reported rather than raised; the caller warns and falls back
    except Exception as error:
        # WARNING with the traceback here, the caller's own summary line
        # separately — the split ``purge_connection`` already uses. The report
        # carries a redacted one-liner, which is what a report can carry; the
        # traceback is what an operator needs and only a log can hold.
        logger.warning(
            "harness teardown: could not publish the connection-delete workflow "
            "for %s",
            qualified_name,
            exc_info=True,
        )
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            errors=(
                "could not publish the connection-delete workflow: "
                f"{sanitize_cause_repr(error)}",
            ),
        )

    # Straight to AE, not through Heracles' package-workflows. The graph AE
    # has to run is the one published above, and Heracles' native path
    # re-derives the graph from an app's manifest and publishes it over exactly
    # that — see FND-1775 and ``_dag``'s historical note. Nothing names an app
    # here, so there is no payload: the node carries its three values as
    # literals and no token needs substituting.
    retry = plan.submit_retry
    try:
        if retry is None:
            ae_run_id = await ae.submit_published_version(seeded.slug)
        else:
            ae_run_id = await ae.submit_published_version(
                seeded.slug,
                retries=retry.retries,
                retry_sleep_seconds=retry.sleep_seconds,
            )
    # conformance: ignore[E004] teardown boundary — see above
    except Exception as error:
        logger.warning(
            "harness teardown: could not submit the connection-delete run for "
            "%s (slug=%s)",
            qualified_name,
            seeded.slug,
            exc_info=True,
        )
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            ae_workflow_slug=seeded.slug,
            errors=(
                f"could not submit the connection-delete run: "
                f"{sanitize_cause_repr(error)}",
            ),
        )

    foreign = await ae.foreign_published_dag(
        seeded.slug, expected=(CONNECTION_DELETE_NODE_ID,)
    )
    if foreign:
        # Before the poll, because the poll is the expensive half: a graph
        # that is not this node is some app's crawl, which takes minutes to fail
        # (or, worse, succeeds) while the connection this call exists to delete
        # sits there.
        logger.warning(
            "harness teardown: the DAG AE will run for %s is not the %r node "
            "this teardown published (slug=%s run_id=%s) — something "
            "republished over the version it submitted, which should be "
            "impossible on the AE-native submit. Not polling it: %s",
            qualified_name,
            CONNECTION_DELETE_NODE_ID,
            seeded.slug,
            ae_run_id,
            foreign,
        )
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            dag_superseded=True,
            ae_workflow_slug=seeded.slug,
            ae_run_id=ae_run_id,
            errors=(
                f"the published DAG carries no {CONNECTION_DELETE_NODE_ID!r} "
                f"node: {foreign}",
            ),
        )

    try:
        result = await ae.poll_native_status(
            ae_run_id,
            interval_seconds=plan.poll_interval_seconds,
            timeout_seconds=plan.poll_timeout_seconds,
            stall_grace_seconds=plan.stall_grace_seconds or None,
            stall_task_queue=plan.task_queue,
            # See the docstring: a long-draining single node is indistinguishable
            # from a wedged one to a glyph comparison.
            progress_stall_seconds=None,
        )
    except NoWorkerOnTaskQueueError as error:
        # The one failure worth its own field. Everything else is "the delete
        # did not work"; this is "the app that does the delete is not here".
        # WARNING here for the traceback; the caller turns the same fact into
        # the line that names the remediation.
        logger.warning(
            "harness teardown: nothing polled %s within %ds, so the "
            "connection-delete app is not running on this tenant and %s was "
            "not deleted through it",
            plan.task_queue,
            plan.stall_grace_seconds,
            qualified_name,
            exc_info=True,
        )
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            app_absent=True,
            ae_workflow_slug=seeded.slug,
            ae_run_id=ae_run_id,
            errors=(
                f"nothing polled {plan.task_queue} within "
                f"{plan.stall_grace_seconds}s: {sanitize_cause_repr(error)}",
            ),
        )
    # conformance: ignore[E004] teardown boundary — see above
    except Exception as error:
        logger.warning(
            "harness teardown: the connection-delete run for %s reached no "
            "verdict (slug=%s run_id=%s)",
            qualified_name,
            seeded.slug,
            ae_run_id,
            exc_info=True,
        )
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            ae_workflow_slug=seeded.slug,
            ae_run_id=ae_run_id,
            errors=(
                f"the connection-delete run did not reach a verdict: "
                f"{sanitize_cause_repr(error)}",
            ),
        )

    ran = {node.name for node in result.nodes}
    if ran and ran != {CONNECTION_DELETE_NODE_ID}:
        # The pre-poll read can be too early — a substitution would land
        # around the submit, and an unreadable or not-yet-updated version
        # answers "unanswered" by design. The names on the run itself cannot be
        # early, and they are checked *before* success: a superseded run that
        # happens to go green would otherwise be reported as a delete that
        # never happened.
        logger.warning(
            "harness teardown: the run AE executed for %s ran node(s) %s "
            "rather than the %r node this teardown published (slug=%s "
            "run_id=%s) — something republished over the version it submitted, "
            "so this run is some other app's DAG and %s was not deleted "
            "through it",
            qualified_name,
            ", ".join(sorted(ran)),
            CONNECTION_DELETE_NODE_ID,
            seeded.slug,
            ae_run_id,
            qualified_name,
        )
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            dag_superseded=True,
            ae_workflow_slug=seeded.slug,
            ae_run_id=ae_run_id,
            errors=(
                "the run executed node(s) "
                f"{', '.join(sorted(ran))} rather than this teardown's "
                f"{CONNECTION_DELETE_NODE_ID} node "
                f"(AE status={result.status.value})",
            ),
        )
    if result.stopped_watching:
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            ae_workflow_slug=seeded.slug,
            ae_run_id=ae_run_id,
            errors=(
                f"the connection-delete run had not finished after "
                f"{plan.poll_timeout_seconds}s (last seen: {result.fingerprint})",
            ),
        )
    if not result.all_nodes_succeeded:
        return ConnectionDeleteReport(
            qualified_name=qualified_name,
            ae_workflow_slug=seeded.slug,
            ae_run_id=ae_run_id,
            errors=(
                f"the connection-delete run did not succeed: "
                f"AE status={result.status.value} nodes={result.fingerprint}",
            ),
        )

    logger.info(
        "e2e cleanup: %s deleted %s (delete_type=%s) via slug=%s run_id=%s",
        CONNECTION_DELETE_WORKFLOW_TYPE,
        qualified_name,
        plan.delete_type.value,
        seeded.slug,
        ae_run_id,
    )
    return ConnectionDeleteReport(
        qualified_name=qualified_name,
        succeeded=True,
        ae_workflow_slug=seeded.slug,
        ae_run_id=ae_run_id,
    )
