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

**The submit that carries this DAG must name no app.** At submit, Heracles
fetches the manifest served at ``metadata.app_service_url`` and publishes it
*over* the workflow's published version — the mechanism
:meth:`~application_sdk.testing.e2e.base.BaseE2ETest._assert_deployed_manifest_matches`
exists to assert on, and the reason the connector's own seed DAG is described as
a placeholder. A teardown is the opposite case: the DAG it publishes **is** the
graph that has to run, so anything published over it replaces the delete with
the app under test's own crawl.

That is not hypothetical. FND-1724 shipped this submit carrying the app under
test's ``app_service_url``, and the two e2e legs of one SDK commit split on the
shape of the app:

* A **bundle** app (metabase) serves no *bare* manifest — only per-entrypoint
  ones — so Heracles' fetch 404'd, nothing superseded the seed, and the
  ``connection-delete`` node ran and purged the connection in 30s.
* A **single-entrypoint** app (openapi) serves one, so Heracles published
  openapi's own two-node graph over the seed and the "teardown" re-ran
  ``extract`` → ``publish`` against a connection it was supposed to delete. It
  failed on the absent credential, and the runner-side purge picked up the
  Atlas half.

**Why not name the delete app's own URL instead**, which does exist — its
namespace carries ``service/connection-delete`` on :8000, the usual
``http://<app>.<app>-app.svc.cluster.local`` shape. Two reasons, and the second
is the disqualifying one:

* Its ``connection-delete-server`` deployment sits at ``0/0`` between runs (the
  same scale-to-zero that makes an unpolled queue normal here), so whether the
  fetch resolves at all depends on whether it wakes in time — which would make
  *which DAG runs* a race rather than a property of the submit.
* A fetch that did resolve would replace this node with the app's manifest
  graph, whose ``delete_type`` default is ``SOFT``. Teardown needs ``PURGE``
  (see :class:`DeleteType`), and nothing in the submit parameters overrides a
  manifest default. Winning that race would archive every run's assets instead
  of removing them, and leave them answering searches on a shared tenant.

Handing the app its own manifest is still the better end state — no
hand-authored graph to drift — but it needs that app's mustache tokens read off
its manifest first, so ``delete_type=PURGE`` can ride the submit. That is
follow-up work, not a swap.

So the node was never wrong; the envelope was, and it worked on exactly the apps
whose manifest endpoint happened to fail. Omitting ``app_service_url`` is what
makes "our DAG runs" a property of the submit rather than of the app under
test's manifest routing. :func:`application_sdk.testing.harness.teardown.delete_connection`
does not take that on trust either — it reads back what AE serves, and reports
a supersede instead of polling a run that is not its own.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef

__all__ = [
    "CONNECTION_DELETE_APP_NAME",
    "CONNECTION_DELETE_NODE_ID",
    "CONNECTION_DELETE_WORKFLOW_TYPE",
    "DeleteType",
    "build_connection_delete_dag",
    "build_connection_delete_submit_payload",
    "connection_delete_task_queue",
]

#: The DAG's single node id. Named for the app rather than for "teardown" so a
#: run list says which app ran, the way ``seed-publish`` does.
CONNECTION_DELETE_NODE_ID = "connection-delete"

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
                },
            },
        }
    }


def build_connection_delete_submit_payload(
    *,
    connection_qualified_name: str,
    connector_short_name: str,
    display_name: str,
    run_id: int,
    ae_workflow_slug: str,
) -> dict[str, Any]:
    """Build the AE submit body for one connection's delete run.

    The same builder the connector's own submit and the seed's submit use, for
    the reason stated there: this DAG carries no mustache tokens and no
    credential, so the body reduces to the envelope plus the connection rows —
    but a second builder would be a second place for AE's submit shape to drift,
    on a path exercised far less often than the connector's.

    **It names no app**, which is the one place this body must *differ* from the
    connector's: ``app_service_url=None``. See the module docstring — a submit
    that names the app under test has that app's manifest published over this
    DAG, and the ``connection-delete`` node never runs.

    Args:
        connection_qualified_name: The connection being deleted.
        connector_short_name: The suite under test — names the AE workflow and
            its labels, so a teardown run in an AE run list is attributable to a
            leg. Not read by the node, which takes the QN as a literal.
        display_name: Human-readable name for the connection rows.
        run_id: This leg's run identifier.
        ae_workflow_slug: The slug AE minted on the create.

    Returns:
        The dict to POST to ``/api/service/package-workflows?submit=true``.
    """
    # Imported here rather than at module scope for the reason the seed's
    # builder states: ``application_sdk.testing.e2e`` imports ``base``, which
    # imports this package, so a top-level import closes a cycle through a
    # partially-initialised package.
    from application_sdk.testing.e2e.payload import (  # noqa: PLC0415
        ConnectionSpec,
        RunMode,
        build_ae_payload,
    )
    from application_sdk.testing.e2e.substitutions import (  # noqa: PLC0415
        MustacheSubstitutions,
    )

    connection = ConnectionSpec(
        name=display_name,
        qualified_name=connection_qualified_name,
        connector_name=connector_short_name,
        source_logo=f"https://assets.atlan.com/assets/{connector_short_name}.png",
    )
    return build_ae_payload(
        run_id=run_id,
        mode=RunMode.DIRECT,
        connector_short_name=connector_short_name,
        argo_package_name=f"@atlan/{connector_short_name}",
        argo_template_name=f"atlan-{connector_short_name}",
        # Not the app under test's URL, and not "" — no key at all. The
        # module docstring has the mechanism.
        app_service_url=None,
        connection=connection,
        mustache_subs=MustacheSubstitutions.model_validate(
            {
                "{{connection}}": ConnectionRef(
                    attributes=ConnectionAttributes(
                        qualified_name=connection_qualified_name, name=display_name
                    )
                ),
                # No ``payload[]`` rides this submit, so nothing would
                # substitute the default ``{{credentialGuid}}`` token — and an
                # unsubstituted token is what ``submit_workflow`` warns about. A
                # teardown has no credential to create: it names a connection,
                # not a source.
                "{{credential-guid}}": "",
            }
        ),
        credential_body=None,
        ae_workflow_slug=ae_workflow_slug,
    )
