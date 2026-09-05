"""The one-node ``PublishWorkflow`` DAG a seed runs, and where it reads from.

Seeding goes *through* publish rather than around it because the failure class
it exists to clear has two halves, and only one of them is an Atlas entity:

1. **Ref emission.** A lineage connector that finds no connection cache either
   emits every ref unvalidated (coalesce sets ``cache_unavailable``) or falls
   back to PartialObjects (mode). The cache is what makes emission correct.
2. **Ref resolution.** The emitted ref then has to bind to something in Atlas.

``build_connection_cache`` in ``atlan-publish-app`` builds that cache from a
connection's *own transformed JSONL* — it does not snapshot arbitrary
connections out of Atlas — so writing skeletons with ``asset.save`` produces
half of what is needed and nothing that fixes the other half. Handing publish a
``transformed_data_prefix`` produces both, from the producer that owns them, with
no artifact the harness has to author or keep in sync.

``publish`` needs no new deployment for this: it is a platform service already
on every tenant, addressed exactly as the connector's own DAG addresses it (see
:func:`application_sdk.testing.e2e.payload.build_seed_dag`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef
from application_sdk.testing.harness.seed._ndjson import connection_entity
from application_sdk.testing.harness.seed._spec import ResolvedSeedSpec

__all__ = [
    "SEED_PUBLISH_NODE_ID",
    "SeedPrefixes",
    "build_seed_publish_dag",
    "build_seed_submit_payload",
]

#: The DAG's single node id. Named for what it is rather than "publish" so a
#: seed run and the connector's own run are never confused in an AE run list or
#: in a required-node assertion.
SEED_PUBLISH_NODE_ID = "seed-publish"


@dataclass(frozen=True, slots=True, kw_only=True)
class SeedPrefixes:
    """The three object-store prefixes one seed publish reads and writes.

    ``atlan-publish-app`` fails its own config validation when
    ``current_state_prefix`` equals ``transformed_data_prefix`` (DBBI-566), so
    the three are siblings under one root rather than aliases of it. Deriving
    them from a single root is what keeps teardown a single ``delete_prefix``.

    Attributes:
        root: The prefix everything for this seed hangs under.
    """

    root: str

    @property
    def transformed(self) -> str:
        """Where the seed's NDJSON is uploaded, and what publish reads."""
        return f"{self.root}/transformed"

    @property
    def publish_state(self) -> str:
        """Where publish keeps this seed's publish-state cache."""
        return f"{self.root}/publish-state"

    @property
    def current_state(self) -> str:
        """Where publish keeps this seed's current-state snapshot."""
        return f"{self.root}/current-state"


def seed_prefix_root(*, app_name: str, qualified_name: str) -> str:
    """Compose the object-store root for one seeded connection.

    Under ``artifacts/apps/`` because that is where every run-scoped artifact in
    the fleet lives, and keyed on the seeded connection's unique suffix rather
    than on a workflow id: the seed exists before any run does, and the suffix is
    the one value that is already unique per test *instance* (see
    :meth:`~application_sdk.testing.harness.identity.Minter.connection_identity`).

    Args:
        app_name: Short name of the connector under test — the leg that owns
            this seed, so a stray prefix is attributable.
        qualified_name: The seeded connection's qualified name.

    Returns:
        The prefix root.
    """
    return f"artifacts/apps/{app_name}/e2e-seed/{qualified_name.rsplit('/', 1)[-1]}"


def build_seed_publish_dag(
    *,
    spec: ResolvedSeedSpec,
    prefixes: SeedPrefixes,
    publish_task_queue: str,
) -> dict[str, Any]:
    """Build the single-node DAG that publishes the seed.

    Every argument is a literal. The connector's own DAG threads
    ``$.extract.outputs.*`` references from the node that produced them; there is
    no producing node here, and a seed whose prefixes were references would be a
    seed whose inputs the harness could not state.

    Args:
        spec: The resolved spec — its connection identity and ACL.
        prefixes: Where the NDJSON was written and where publish keeps state.
        publish_task_queue: The tenant's publish queue, e.g.
            ``atlan-publish-production``.

    Returns:
        The graph to publish as the AE workflow's seed version.
    """
    return {
        SEED_PUBLISH_NODE_ID: {
            "node_type": "workflow",
            "activity_name": "execute_workflow",
            "activity_display_name": "Seed lineage parents via Publish",
            "app_name": "publish",
            "app_task_queue": publish_task_queue,
            "inputs": {
                "workflow_type": "PublishWorkflow",
                "task_queue": publish_task_queue,
                "args": {
                    "connection_qualified_name": spec.qualified_name,
                    "transformed_data_prefix": prefixes.transformed,
                    "publish_state_prefix": prefixes.publish_state,
                    "current_state_prefix": prefixes.current_state,
                    # The connection does not exist yet — publish creates it from
                    # ``connection_entity`` and then waits for its access policies
                    # to sync, which is the wait a direct pyatlan seed had to
                    # reimplement as a 403-retry loop.
                    "connection_creation_enabled": True,
                    "connection_entity": connection_entity(spec),
                    # The half a pyatlan write could never produce: the SQLite the
                    # consuming connector resolves its refs against. Without it a
                    # cache-consuming connector emits unvalidated refs (coalesce)
                    # or PartialObjects (mode), and the seeded entities go unused.
                    "connection_cache_via_app_enabled": True,
                },
            },
        }
    }


def build_seed_submit_payload(
    *,
    spec: ResolvedSeedSpec,
    run_id: int,
    ae_workflow_slug: str,
    app_service_url: str,
) -> dict[str, Any]:
    """Build the AE submit body for a seed's publish run.

    Deliberately the *same* builder the connector's own submit uses. The seed's
    DAG carries no mustache tokens and no credential, so the body reduces to the
    envelope plus the connection rows — but routing it through a second builder
    would be a second place for AE's submit shape to drift, on a path that is
    exercised far less often than the connector's.

    Args:
        spec: The resolved spec, which is also the seeded connection's identity.
        run_id: This leg's run identifier, for the AE workflow name and labels.
        ae_workflow_slug: The slug AE minted on the create.
        app_service_url: HTTP URL AE can reach the app at.

    Returns:
        The dict to POST to ``/api/service/package-workflows?submit=true``.
    """
    # Imported here rather than at module scope: ``application_sdk.testing.e2e``
    # imports ``base``, which imports this package, so a top-level import of an
    # ``e2e`` submodule closes a cycle through a partially-initialised package.
    from application_sdk.testing.e2e.payload import (  # noqa: PLC0415
        ConnectionSpec,
        RunMode,
        build_ae_payload,
    )
    from application_sdk.testing.e2e.substitutions import (  # noqa: PLC0415
        MustacheSubstitutions,
    )

    connector = spec.connector_type
    connection = ConnectionSpec(
        name=spec.display_name,
        qualified_name=spec.qualified_name,
        connector_name=connector,
        source_logo=f"https://assets.atlan.com/assets/{connector}.png",
        admin_users=spec.admin_users,
        admin_groups=spec.admin_groups,
        admin_roles=spec.admin_roles,
    )
    return build_ae_payload(
        run_id=run_id,
        mode=RunMode.DIRECT,
        connector_short_name=connector,
        argo_package_name=f"@atlan/{connector}",
        argo_template_name=f"atlan-{connector}",
        app_service_url=app_service_url,
        connection=connection,
        # Built through the aliases rather than the field names because the
        # aliases *are* the manifest mustache literals this model exists to
        # express (see its class docstring) — and they are the names its
        # ``__init__`` actually takes.
        mustache_subs=MustacheSubstitutions.model_validate(
            {
                "{{connection}}": ConnectionRef(
                    attributes=ConnectionAttributes(
                        qualified_name=spec.qualified_name, name=spec.display_name
                    )
                ),
                # No ``payload[]`` rides this submit, so nothing would substitute
                # the default ``{{credentialGuid}}`` token — and an
                # unsubstituted token is what ``submit_workflow`` warns about. A
                # seed has no credential to create: it reads an object-store
                # prefix, not a source.
                "{{credential-guid}}": "",
            }
        ),
        credential_body=None,
        ae_workflow_slug=ae_workflow_slug,
    )
