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

**The submit must name the delete app, not the app under test, and the DAG must
be the delete app's own.** At submit, Heracles fetches a manifest and publishes
it *over* the workflow's published version — the mechanism
:meth:`~application_sdk.testing.e2e.base.BaseE2ETest._assert_deployed_manifest_matches`
exists to assert on, and the reason a connector's seed DAG is described as a
placeholder. Which app it fetches comes off the submit envelope's *identity*
(``package.argoproj.io/name``, ``atlanName``, ``templateRef``), so a teardown
that carries the suite's identity has the suite's crawl published over its
delete.

**Whether the republish beats the run is a race, and that is the load-bearing
fact.** FND-1724 first shipped this submit carrying the app under test's
identity, and one SDK commit produced both outcomes on the same app *and* the
same tenant — openapi's ``connection-create-gcp`` leg ran the delete while its
``connection-reuse-gcp`` leg had ``extract`` → ``publish`` published over it
minutes later. Omitting ``app_service_url`` (the first fix attempt) changed
nothing, which is what proved the fetch is not keyed on that field. A race
cannot be won by naming the field differently; it can only be made *harmless*,
by making both possible outcomes a delete:

* Heracles' fetch wins → the graph that runs is the delete app's own manifest
  DAG, with its ``{{connection-qualified-name}}`` / ``{{delete-type}}`` /
  ``{{delete-assets}}`` tokens substituted from this submit's parameter rows.
* The seed survives → the graph that runs is this module's copy of that same
  node, with the same three values as literals.

Which is why :data:`CONNECTION_DELETE_NODE_ID` is the app manifest's own node id
and every other field is copied from it verbatim: the two versions are meant to
be indistinguishable. The guard in
:func:`application_sdk.testing.harness.teardown.delete_connection` then has one
job left — catching a *third* graph, which is no longer a race but a bug.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef

__all__ = [
    "CONNECTION_DELETE_APP_NAME",
    "CONNECTION_DELETE_NODE_ID",
    "CONNECTION_DELETE_PACKAGE_NAME",
    "CONNECTION_DELETE_TEMPLATE_NAME",
    "CONNECTION_DELETE_WORKFLOW_TYPE",
    "DeleteType",
    "build_connection_delete_dag",
    "build_connection_delete_submit_payload",
    "connection_delete_task_queue",
]

#: The DAG's single node id, taken from the app's own
#: ``app/generated/manifest.json``. **Not** a teardown-flavoured name: whichever
#: of the two versions AE ends up running (see the module docstring's race), the
#: node has to be the same one, or the harness would have to tell a delete it
#: published from a delete the app published.
CONNECTION_DELETE_NODE_ID = "delete"

#: Envelope identity — ``package.argoproj.io/name`` and, through
#: ``connector_short_name``, ``atlanName``, the run label and
#: ``metadata.name``. This is what decides *which app's manifest* Heracles
#: fetches and publishes over the seed, so it names the delete app rather than
#: the suite under test. Attribution to a leg is not lost: it lives on the AE
#: workflow's name and description, which
#: :class:`~application_sdk.testing.harness.teardown.ConnectionDeletePlan`
#: composes from the connector.
CONNECTION_DELETE_PACKAGE_NAME = "@atlan/connection-delete"

#: The cluster-scoped ``templateRef`` name, on the same rule as
#: :data:`CONNECTION_DELETE_PACKAGE_NAME`. Native execution does not run an Argo
#: template, but the envelope carries one and it must not name the suite's.
CONNECTION_DELETE_TEMPLATE_NAME = "atlan-connection-delete"

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
                    # Also on the app's manifest, inside ``args`` as well as on
                    # the node. Kept because the goal is a node byte-identical
                    # to the one the tenant runs from the marketplace, not a
                    # minimal one — see the module docstring on why the two
                    # versions must be indistinguishable.
                    "app_name": CONNECTION_DELETE_APP_NAME,
                },
            },
            "error_handling": {
                "start_to_close_timeout_seconds": CONNECTION_DELETE_TIMEOUT_SECONDS
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
    delete_type: DeleteType = DeleteType.PURGE,
    delete_assets: bool = True,
) -> dict[str, Any]:
    """Build the AE submit body for one connection's delete run.

    The same builder the connector's own submit and the seed's submit use, for
    the reason stated there: this DAG carries no mustache tokens and no
    credential, so the body reduces to the envelope plus the connection rows —
    but a second builder would be a second place for AE's submit shape to drift,
    on a path exercised far less often than the connector's.

    **It names the delete app, not the suite**, which is the one place this body
    must differ from the connector's — the envelope's identity is what decides
    which manifest Heracles fetches and publishes over the seed, and the module
    docstring has the evidence. Three consequences, all deliberate:

    * ``package.argoproj.io/name`` is :data:`CONNECTION_DELETE_PACKAGE_NAME` and
      ``templateRef`` is :data:`CONNECTION_DELETE_TEMPLATE_NAME`, so a fetch that
      resolves resolves to the delete app.
    * the parameter rows carry
      :class:`~application_sdk.testing.e2e.substitutions.ConnectionDeleteSubstitutions`,
      so that manifest's ``{{connection-qualified-name}}`` / ``{{delete-type}}``
      / ``{{delete-assets}}`` tokens resolve to *this* teardown's values instead
      of the app's ``SOFT`` default.
    * ``app_service_url`` stays absent. Omitting it was the first fix attempt and
      it did not stop the republish, which is what proved the fetch is not keyed
      on it — but a teardown still has no reason to name an address, and a
      guessed in-cluster URL would be one more thing to be wrong.

    Args:
        connection_qualified_name: The connection being deleted.
        connector_short_name: The suite under test. Names the connection rows
            (``connectorName``, the source logo), so an AE run list still shows
            whose connection this is; it deliberately no longer reaches the
            envelope's package or template.
        display_name: Human-readable name for the connection rows.
        run_id: This leg's run identifier.
        ae_workflow_slug: The slug AE minted on the create.
        delete_type: How thoroughly to delete, for the substitution rows. The
            node's own literal comes from :func:`build_connection_delete_dag`,
            and both have to say the same thing — which is why the caller passes
            one value into both rather than each defaulting on its own.
        delete_assets: Whether to drain the connection's assets first, likewise.

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
        ConnectionDeleteSubstitutions,
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
        # The delete app is what this submit runs, so it is what the envelope
        # names — labels, ``atlanName`` and ``metadata.name`` included. The leg
        # stays identifiable through the AE workflow's own name and description,
        # and through the connection rows built above.
        connector_short_name=CONNECTION_DELETE_WORKFLOW_TYPE,
        argo_package_name=CONNECTION_DELETE_PACKAGE_NAME,
        argo_template_name=CONNECTION_DELETE_TEMPLATE_NAME,
        # Not the app under test's URL, and not "" — no key at all. The
        # module docstring has the mechanism.
        app_service_url=None,
        connection=connection,
        mustache_subs=ConnectionDeleteSubstitutions.model_validate(
            {
                # The three the delete app's manifest reads. Present so a
                # republished manifest resolves to this teardown's values; the
                # seed carries the same three as literals.
                "{{connection-qualified-name}}": connection_qualified_name,
                "{{delete-type}}": delete_type,
                "{{delete-assets}}": delete_assets,
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
