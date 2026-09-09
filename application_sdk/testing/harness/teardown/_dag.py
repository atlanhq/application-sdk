"""The one-node ``connection-delete`` DAG teardown runs, and what it is handed.

Teardown goes *through* the app for the same reason
:mod:`application_sdk.testing.harness.seed` seeds through publish: the artifacts
belong to apps, and the harness cannot reach half of them from outside the
tenant.

**The half it cannot reach is not a permissions oversight — it is the shape of
the call.** ``delete_prefix`` is a LIST followed by a bulk ``POST ?delete``, and
both are *bucket-level* URLs. The tenant's Kong s3proxy path-matches every
request against an allowlist (``/persistent-artifacts/``, ``/artifacts/apps/``,
``/workflow_file_upload/``), which it cannot apply to a URL whose keys live in
the request body — so the call comes back ``403 code 1009 "Invalid Path"`` even
when the prefix being deleted is itself allowlisted. The SDK names the same
mechanism at :mod:`application_sdk.storage.preflight`. And
``connection-cache/`` is not on the allowlist at all, so no method reaches it
from a runner.

``connection-delete`` has no such problem: it runs on the tenant, where its
object store is the tenant bucket accessed directly with no proxy in front of
it. One node therefore replaces *both* halves of the old hand-rolled teardown —
the pyatlan purge and the byte-stores — and picks up two artifacts the harness
never knew existed:

* ``connection-cache/<cqn>.sqlite`` — the cache a consuming connector reads.
* ``persistent-artifacts/apps/atlan-publish-app/state/<cqn>/`` — publish's
  per-connection state (``publish-cache-v2`` blue/green, WAL, drift,
  statistics).

**Publish never cleans up its own cache**, which is easy to get backwards. It
only ever writes that prefix; nothing in publish reacts to the assets being
deleted. Connection deletion is the owner, and it is a separate app. Leaving the
state behind is not merely untidy: publish's Step-0 resolve auto-discovers
caches by *connectorType*, not by qualified name, so every orphan from every
prior run is materialised inside the resolver's memory ladder on the next run.

Every argument on the node is a literal, exactly as in
:func:`application_sdk.testing.harness.seed.build_seed_publish_dag`: there is no
producing node to thread ``$.<node>.outputs.*`` references from, and a teardown
whose target came from a reference would be a teardown the harness could not
state.

**The harness submits this DAG straight to AE**, through
:meth:`~application_sdk.testing.harness.automation_engine.AEClient.submit_published_version`
(``POST /automation/api/v1/workflows/<slug>/submit``), which starts the version
published two calls earlier and fetches no manifest. The graph that runs is
therefore the graph built here: no envelope, no app identity, and no second
candidate. Which is what leaves this module a DAG builder and nothing else —
there is no submit body to build, because a submit that names nothing has no
body to get right.

**Historical note, because believing otherwise leads straight back to a
disproved fix.** Until FND-1775 this teardown submitted through Heracles'
``/api/service/package-workflows``, whose native path *always* re-derives the
graph from an app's served manifest and publishes it over the harness's own
version. FND-1724 could not stop that, so it made it harmless: the envelope
named the delete app and this node was a verbatim copy of that app's manifest
node, so whichever version AE ended up serving was a delete either way.
FND-1766 then established that the deciding variable was metastore replication
lag rather than a race between two writers, and that submitting to AE removes
the class outright rather than defusing one instance of it. The envelope
builder, the delete app's package and template names, and
``ConnectionDeleteSubstitutions`` went with it.

What survives of that design is :data:`CONNECTION_DELETE_NODE_ID`'s *value* and
the read-back guard in
:func:`application_sdk.testing.harness.teardown.delete_connection`. The guard is
not left over: it is the assertion that the determinism above holds on a live
tenant, on the same rule as
:class:`~application_sdk.testing.harness.seed.SeedDagSupersededError` — it
should now be unreachable, and being unreachable is what it is for.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = [
    "CONNECTION_DELETE_APP_NAME",
    "CONNECTION_DELETE_NODE_ID",
    "CONNECTION_DELETE_WORKFLOW_TYPE",
    "DeleteType",
    "build_connection_delete_dag",
    "connection_delete_task_queue",
]

#: The DAG's single node id, taken from the app's own
#: ``app/generated/manifest.json``. It was verbatim because FND-1724 needed the
#: node the harness published and the node the app published to be
#: indistinguishable (the module docstring's historical note has why); it stays
#: verbatim now only because renaming a released constant buys nothing.
CONNECTION_DELETE_NODE_ID = "delete"

#: The app's own ``start_to_close`` for the delete activity — three days, from
#: its manifest's ``error_handling``. Copied rather than left to AE's default
#: because draining a large connection is what the number is sized for, and the
#: harness's own poll ceiling
#: (:attr:`~application_sdk.testing.harness.teardown.ConnectionDeletePlan.poll_timeout_seconds`)
#: is the bound that actually stops an e2e leg waiting.
CONNECTION_DELETE_TIMEOUT_SECONDS = 259200

#: What the app's worker registers, per ``app/generated/manifest.json`` in
#: ``atlanhq/atlan-connection-delete-app``.
CONNECTION_DELETE_WORKFLOW_TYPE = "connection-delete"

#: Node-level ``app_name``. The app's own generated manifest declares
#: ``automation-engine`` here — the same value this SDK's proven ``lineage-app``
#: node carries (:func:`application_sdk.testing.e2e.payload.build_seed_dag`) —
#: and routing to the worker is done by ``app_task_queue`` regardless. Taking
#: the app's manifest verbatim is what keeps this node identical to the one the
#: tenant runs from the marketplace.
CONNECTION_DELETE_APP_NAME = "automation-engine"


class DeleteType(StrEnum):
    """How thoroughly ``connection-delete`` removes the assets it finds.

    The app validates this string itself and fails the run on anything else
    (``InvalidDeleteTypeError``), so it is an enum here rather than a ``str``:
    a typo that would surface minutes later as a failed teardown node is worth
    catching at the call site instead.

    Attributes:
        SOFT: Archive. Assets stay recoverable in Atlas — the app's own default,
            and the wrong one for an ephemeral e2e connection, which is never
            coming back.
        HARD: Delete without the archive tombstone.
        PURGE: Remove outright. What the harness uses, because it is what the
            ``pyatlan`` purge this replaced did, and a run's leftovers must not
            keep answering searches on a shared tenant.
    """

    SOFT = "SOFT"
    HARD = "HARD"
    PURGE = "PURGE"


def connection_delete_task_queue(deployment_name: str) -> str:
    """Compose the task queue the tenant's ``connection-delete`` app polls.

    ``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``, as every
    platform app on the tenant resolves it — mirroring ``atlan-publish-production``.

    Args:
        deployment_name: The deployment the tenant registers its system apps
            under, e.g. ``production``.

    Returns:
        The queue name.
    """
    return f"atlan-connection-delete-{deployment_name}"


def build_connection_delete_dag(
    *,
    connection_qualified_name: str,
    task_queue: str,
    delete_type: DeleteType = DeleteType.PURGE,
    delete_assets: bool = True,
) -> dict[str, Any]:
    """Build the single-node DAG that deletes one connection and its artifacts.

    One node per connection rather than one DAG carrying every node, because the
    two properties the hand-rolled teardown had are both worth keeping and no
    single graph has both. Chained ``depends_on`` nodes would preserve the
    ordering (a run's own connection holds lineage refs *into* the seeded
    skeletons, and deleting the referrer first is the direction that cannot
    strand an edge) but let one stuck delete orphan every later one; parallel
    nodes would keep them independent and lose the ordering. Submitting one
    single-node run per connection, in order, keeps both.

    Args:
        connection_qualified_name: The connection to delete. Must be a name the
            run minted for itself — never a long-lived shared connection, whose
            assets are not this run's to delete, and which no guard downstream
            can tell apart from an ephemeral one.
        task_queue: The tenant's connection-delete queue, from
            :func:`connection_delete_task_queue`.
        delete_type: How thoroughly to delete. ``PURGE`` for e2e teardown.
        delete_assets: Whether to drain the connection's assets before deleting
            the Connection entity. ``False`` deletes only the entity, which
            strands everything under it — present because the app's contract has
            it, not because a teardown should ever pass it.

    Returns:
        The graph to publish as the AE workflow's seed version.
    """
    return {
        CONNECTION_DELETE_NODE_ID: {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "Delete Connection & Assets",
            "app_name": CONNECTION_DELETE_APP_NAME,
            "app_task_queue": task_queue,
            "inputs": {
                "workflow_type": CONNECTION_DELETE_WORKFLOW_TYPE,
                "app_name": CONNECTION_DELETE_APP_NAME,
                "task_queue": task_queue,
                "args": {
                    # The three fields the app's UI form carries, and the only
                    # ones its top-level input reads. Storage params
                    # (bucket/cloud/azure) and search tuning
                    # (chunk_size/delete_size) live on its per-task contracts
                    # with their own defaults, and are deliberately not stated
                    # here: a teardown that pinned them would drift from the
                    # app the moment it retuned them.
                    "connection_qualified_name": connection_qualified_name,
                    "delete_type": delete_type.value,
                    "delete_assets": delete_assets,
                    # Also on the app's manifest, inside ``args`` as well as
                    # on the node. Kept even though FND-1775 removed the reason
                    # the whole node had to be byte-identical: whether the app's
                    # own input model reads it here is the app's business, and
                    # copying its manifest is how this node stays correct
                    # without asserting on internals the harness cannot see.
                    "app_name": CONNECTION_DELETE_APP_NAME,
                },
            },
            "error_handling": {
                "start_to_close_timeout_seconds": CONNECTION_DELETE_TIMEOUT_SECONDS
            },
        }
    }
