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

**There is no submit payload here, and that absence is the fix for FND-1766.**
A submit through Heracles' ``/api/service/package-workflows`` carries an
envelope naming an app, and Heracles' native path re-derives the graph from that
app's served manifest and publishes it over whatever the caller published — so
the seed's node was replaced by the connector's own ``extract`` / ``publish``
whenever AE's submit had already seen the republish. The seed therefore submits
straight to AE
(:meth:`~application_sdk.testing.harness.automation_engine.AEClient.submit_published_version`),
which runs the currently published version and fetches no manifest. Nothing
names an app, so nothing can name the wrong one.

Picking a *different* identity was the other candidate and it does not work:
``atlan-publish-app`` is a marketplace app (``atlan.yaml``: ``name: publish``),
but it has no ``app/generated/``, no programmatic manifest and no
``@entrypoint``, so its ``/workflows/v1/manifest`` answers 404 — and Heracles
treats a failed manifest fetch as fatal *before* it writes anything, so a seed
submit naming publish is an unconditional HTTP 500 rather than a seed that
survives by absence.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from application_sdk.testing.harness.seed._ndjson import (
    TRANSFORMED_FILE_NAME,
    connection_entity,
)
from application_sdk.testing.harness.seed._spec import ResolvedSeedSpec

__all__ = [
    "SEED_PUBLISH_NODE_ID",
    "SeedPrefixes",
    "build_seed_publish_dag",
    "seed_object_keys",
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
    them from a single root is what lets a caller name any of them from the one
    value teardown carries — see :func:`seed_object_keys`, which composes the
    key it deletes out of exactly this.

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
    the fleet lives, and keyed on the connection's **whole** qualified name — not
    on its last segment, and not on a workflow id (the seed exists before any run
    does).

    The whole QN, percent-encoded into a single path segment, is doing two jobs
    that the trailing segment alone did neither of:

    * **No collisions.** ``SeedSpec.qualified_name`` is caller-supplied, so the
      per-instance uniqueness
      :meth:`~application_sdk.testing.harness.identity.Minter.connection_identity`
      guarantees is a property of the *default*, not of the input. Keyed on the
      suffix, ``default/snowflake/123`` and ``default/postgres/123`` share one
      prefix — two seeds writing over each other's NDJSON, and either one's
      teardown deleting both.
    * **No nesting.** Encoding rather than nesting the QN keeps each root exactly
      one segment deep, so no seed's root can ever be a path prefix of another's
      — which is what would put one seed's keys inside another seed's tree, and
      one seed's teardown on top of a sibling's bytes.

    ``quote(..., safe="")`` encodes ``%`` as ``%25``, so the mapping is injective
    for any input rather than only for the shapes we expect. The result still
    reads as the QN in a bucket listing, which is what keeps a stray prefix
    attributable.

    Args:
        app_name: Short name of the connector under test — the leg that owns
            this seed, so a stray prefix is attributable to a leg as well as to
            a connection.
        qualified_name: The seeded connection's qualified name.

    Returns:
        The prefix root.
    """
    encoded = urllib.parse.quote(qualified_name, safe="")
    return f"artifacts/apps/{app_name}/e2e-seed/{encoded}"


def seed_object_keys(*, root: str) -> tuple[str, ...]:
    """Every object-store key the *harness* wrote under one seed root.

    Teardown deletes these one key at a time rather than deleting the root as a
    prefix, because ``delete_prefix`` cannot work from a runner at all:
    it is a LIST plus a bulk ``POST ?delete``, both *bucket-level* URLs, and
    the tenant's Kong s3proxy path-matches against an allowlist it cannot apply
    to a URL whose keys live in the request body. That call comes back
    ``403 code 1009 "Invalid Path"`` even though ``/artifacts/apps/`` — the
    prefix these keys sit under — is on that allowlist.

    **The per-key DELETE does not get through either, and this docstring used to
    claim it did.** Putting the key in the path was the expected fix — the
    allowlist can read a path — but a live e2e run on 2026-09-07 (FND-1766's
    three-cloud A/B) came back ``403`` on the single-object DELETE of
    ``artifacts/apps/<app>/e2e-seed/<qn>/transformed/assets.json`` as well. So
    the allowlist is not refusing a *URL shape*; it does not grant DELETE under
    ``/artifacts/apps/`` to a runner in any form. No rearrangement of the
    request from outside the tenant will fix that, and the next reader should
    not spend the afternoon finding a third URL shape.

    Which makes this function's remaining value the *enumeration*, not the
    deletion: it is the one place that states what a seed writes, and
    :func:`~application_sdk.testing.harness.seed.seed_assets` composes the same
    key from :class:`SeedPrefixes`. The real fix is to bring the seed root into
    an on-tenant app's scope — ``connection-delete``'s ``archive_storage``,
    which already clears the app-owned per-connection stores — so the keys are
    deleted by something that is not behind the proxy. Until then the delete is
    attempted and its failure logged, which leaves bounded bytes behind rather
    than reding a leg.

    What is deliberately absent is everything *publish* writes under the same
    root (``publish-state/``, ``current-state/``). The harness did not write
    those keys and cannot enumerate them from a runner; clearing them belongs to
    an app running on the tenant.

    Args:
        root: The seed's prefix root, from :func:`seed_prefix_root`.

    Returns:
        The keys, in the order they were written.
    """
    return (f"{SeedPrefixes(root=root).transformed}/{TRANSFORMED_FILE_NAME}",)


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
                    #
                    # THREE flags, not one, and every one of them defaults to
                    # false in atlan-publish-app (`constants.py`, from env). A
                    # seed that sets only the via-app flag publishes entities and
                    # no usable cache — and the entities are what the read-back
                    # checks, so it greens while delivering half the fix:
                    #
                    #   connection_cache_enabled — gates cache construction
                    #     outright (`_try_build_connection_cache`, publish_app.py
                    #     :4213). Unset, nothing is built at all.
                    #   executor_enabled — with the via-app flag, decides
                    #     `connection_cache_dry_run` (publish_app_config.py:398).
                    #     Unset, a built cache is uploaded under
                    #     `connection-cache-dry-run/`, which no connector reads.
                    #   connection_cache_via_app_enabled — publish owns the
                    #     artifact rather than the connector.
                    #
                    # Stated here rather than left to the tenant's env for the
                    # reason every other arg on this node is a literal: a seed
                    # whose behaviour depends on a deployment default is a seed
                    # that means something different on each tenant.
                    "connection_cache_enabled": True,
                    "connection_cache_via_app_enabled": True,
                    "executor_enabled": True,
                },
            },
        }
    }
