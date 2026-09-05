"""Declarative lineage-parent seeding: the assets a run's cross-source refs resolve to.

Why this exists (FND-402 / FND-1648): a lineage-only connector publishes Process
/ ColumnProcess entities whose ``inputs``/``outputs`` reference *another
source's* assets by qualified name — Snowflake tables under a Coalesce run,
warehouse tables under an ADF or Mode run. On a connector-scoped e2e tenant that
other source has never been crawled, so publish rejects every reference
(``ATLAS-404-00-00A``) and the leg fails wholesale: 72 entities on adf, 9 on
mode, 19,210 on coalesce. It is the largest single failure class in the fleet's
e2e rollout.

**Two failure modes stack, and only one of them is an Atlas entity.**

1. *Ref emission* is connector-side and needs the **connection cache**: with no
   cache loaded, coalesce sets ``cache_unavailable`` and emits every ref
   unvalidated, and mode falls back to PartialObjects. All three motivating
   connectors declare ``connection_cache_enabled`` and
   ``connection_cache_via_app_enabled``.
2. *Ref resolution* is Atlas-side and needs the **entity**: the emitted ref has
   to bind to something, by exact-match qualified name and exact type — no fuzzy
   matching, no case folding, and a ``Table`` never resolves a ref that said
   ``View``.

Writing skeleton entities with ``pyatlan``'s ``asset.save`` addresses (2) and
nothing else. ``build_connection_cache`` in ``atlan-publish-app`` builds the
cache from a connection's *own transformed JSONL* — it does not snapshot
arbitrary connections out of Atlas — so a direct write produces no cache, and a
harness-authored cache blob would mean reimplementing a producer we do not own,
with silent drift. This is FND-1147 one connection over: *"it read 'prior crawl'
as 'prior ASSETS' and seeded them with pyatlan, which the lineage app cannot
see."*

**So the seed goes through publish.** :func:`seed_assets` writes transformed
NDJSON to the object store and submits one ``PublishWorkflow`` node, and publish
owns both entities and cache — from the producer that owns them, with no
artifact the harness authors or keeps in sync. ``publish`` needs no new
deployment: it is a platform service already on every tenant, addressed exactly
as the connector's own DAG addresses it.

**When to use this rather than a real crawl.** Run a real crawl when the
referenced source is reachable *inside the leg* — the postgres miner's warehouse
is the same app's other entrypoint, with a hermetic container already in the job,
so it stays a :class:`~application_sdk.testing.e2e.base.DAGSpec` crawl. Use
synthetic-publish seeding only when it is not: coalesce → Snowflake, adf → ADLS /
Cosmos / Salesforce. A crawl seeds from the real producer and its QN parity holds
by construction; this does not, which is why every segment is validated and the
batch is checked offline before it is submitted.

**Exactness is the whole contract.** Every QN segment must match what the
connector under test emits **byte for byte, case included**. A connector that
upper-cases segments (Snowflake refs are ``.upper()``-d) must be seeded
upper-cased; a connector whose warehouse QNs are not config-pinnable must
precompute them from its own source fixture rather than invent them. Derive the
spec from the connector's committed transform goldens where you can — that is QN
parity by construction rather than by hope.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import upload_file
from application_sdk.testing.harness.automation_engine import AEClient
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.seed._errors import (
    SeedPublishFailedError,
    SeedSegmentInvalidError,
    SeedStoreUnavailableError,
    SeedTreeInvalidError,
)
from application_sdk.testing.harness.seed._ndjson import (
    TRANSFORMED_FILE_NAME,
    connection_entity,
    ndjson_bytes,
    skeleton_assets,
    write_transformed_dir,
)
from application_sdk.testing.harness.seed._publish import (
    SEED_PUBLISH_NODE_ID,
    SeedPrefixes,
    build_seed_publish_dag,
    build_seed_submit_payload,
    seed_prefix_root,
)
from application_sdk.testing.harness.seed._spec import (
    DatabaseSpec,
    ResolvedSeedSpec,
    SchemaSpec,
    SeededConnection,
    SeedSpec,
    TableSpec,
    validate_resolved_spec,
)
from application_sdk.testing.harness.starters import (
    AEWorkflowSpec,
    SubmitRetry,
    publish_seed_version,
)
from application_sdk.validation.assets import validate_transformed_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from obstore.store import ObjectStore

logger = get_logger(__name__)

__all__ = [
    "SEED_PUBLISH_NODE_ID",
    "TRANSFORMED_FILE_NAME",
    "DatabaseSpec",
    "ResolvedSeedSpec",
    "SchemaSpec",
    "SeedPrefixes",
    "SeedPublishFailedError",
    "SeedPublishPlan",
    "SeedSegmentInvalidError",
    "SeedSpec",
    "SeedStoreUnavailableError",
    "SeedTreeInvalidError",
    "SeededConnection",
    "TableSpec",
    "build_seed_publish_dag",
    "build_seed_submit_payload",
    "connection_entity",
    "ndjson_bytes",
    "seed_assets",
    "seed_prefix_root",
    "skeleton_assets",
    "validate_resolved_spec",
    "write_transformed_dir",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class SeedPublishPlan:
    """How one seed's publish run is addressed, dispatched and waited on.

    The run-scoped half of a seed, kept apart from :class:`SeedSpec` because the
    spec is the connector's contract (and a unit test's fixture) while this is
    the leg's wiring. A suite declares the first; ``BaseE2ETest`` resolves the
    second from the same class attributes its own run uses.

    Attributes:
        app_name: Short name of the connector under test — names the
            object-store prefix, so a stray seed is attributable to a leg.
        publish_task_queue: The tenant's publish queue, e.g.
            ``atlan-publish-production``.
        ae_workflow_name: Name for the AE workflow this seed runs under. Must not
            collide with the suite's own, or AE would carry two graphs on one
            workflow and the run list would not say which ran.
        app_service_url: HTTP URL AE can reach the app at.
        run_id: This leg's run identifier, for the AE workflow name and labels.
        submit_retry: Cold-start sizing for the submit, or ``None`` to leave
            ``submit_workflow``'s own default budget in place.
        poll_interval_seconds: Gap between ``native-status`` reads.
        poll_timeout_seconds: Ceiling on the whole publish wait.
        progress_stall_seconds: Progress-watchdog window, or ``None`` to disable.
        minter: Supplies the AE seed version. ``None`` mints from the real clock.
    """

    app_name: str
    publish_task_queue: str
    ae_workflow_name: str
    app_service_url: str
    run_id: int
    submit_retry: SubmitRetry | None = None
    poll_interval_seconds: int = 10
    poll_timeout_seconds: int = 1800
    progress_stall_seconds: int | None = None
    minter: Minter | None = None


async def seed_assets(
    spec: ResolvedSeedSpec,
    *,
    store: ObjectStore,
    ae: AEClient,
    plan: SeedPublishPlan,
) -> SeededConnection:
    """Serialise *spec*, validate it offline, and publish it as a real run.

    Five steps, in this order for one reason each:

    1. **Serialise locally.** ``write_transformed_dir`` emits the NDJSON a
       crawler of the referenced source would have emitted.
    2. **Validate offline** with
       :func:`~application_sdk.validation.assets.validate_transformed_dir` and
       referential integrity on — before the upload, so a batch that would
       publish partially never reaches the tenant. Tenant-free and cheap; it is
       what turns "the parents are all there" from hoped-for into asserted.
    3. **Upload** to ``plan``'s prefix.
    4. **Publish**: create and seed an AE workflow carrying the one-node DAG,
       then submit it. Publish creates the Connection (and waits out its own
       policy sync), writes the entities, and builds the connection cache.
    5. **Wait for a verdict**, and refuse anything short of every node
       succeeding — a partially-published seed is exactly the shape that greens a
       leg while dropping lineage.

    Args:
        spec: What to seed, already resolved and validated
            (:meth:`SeedSpec.resolve`).
        store: An object store the tenant's publish app reads. In CI that is the
            configurator-emitted ``atlan-objectstore`` binding.
        ae: An open AE client on *this* event loop. Not closed here — the
            transport's lifetime stays with whoever opened it.
        plan: How this seed is addressed, dispatched and waited on.

    Returns:
        The :class:`SeededConnection`, whose ``qualified_name`` is the prefix to
        rebase the connector's refs onto and whose ``prefix`` is what teardown
        deletes.

    Raises:
        SeedTreeInvalidError: The serialised batch failed per-asset validation or
            referential integrity. Nothing was uploaded and nothing submitted.
        SeedPublishFailedError: The publish run did not succeed on every node.
        AtlanApiHttpError: AE rejected one of the create/seed/publish/submit
            writes.
        AppNotReadyError: The tenant's publish app never accepted the submit
            across the cold-start budget.
    """
    prefixes = SeedPrefixes(
        root=seed_prefix_root(
            app_name=plan.app_name, qualified_name=spec.qualified_name
        )
    )

    with tempfile.TemporaryDirectory(prefix="harness-seed-") as scratch:
        written = write_transformed_dir(spec, Path(scratch))
        report = validate_transformed_dir(
            written.directory, check_referential_integrity=True
        )
        if not report.ok:
            raise SeedTreeInvalidError(
                message=(
                    f"the skeleton tree for {spec.qualified_name} would not "
                    "survive publish, so it was not uploaded or submitted:\n"
                    f"{report.format_report()}"
                ),
                resource=spec.qualified_name,
                actual_state=(
                    f"{report.failed} invalid, {len(report.orphans)} orphaned of "
                    f"{report.total} record(s)"
                ),
            )
        key = f"{prefixes.transformed}/{TRANSFORMED_FILE_NAME}"
        await upload_file(key, str(written.path), store)

    logger.info(
        "harness seed: uploaded %d record(s) for %s to %s",
        report.total,
        spec.qualified_name,
        prefixes.transformed,
    )

    seeded = await publish_seed_version(
        AEWorkflowSpec(
            name=plan.ae_workflow_name,
            description=f"Full-DAG e2e harness — lineage-parent seed for {plan.app_name}",
            seed_dag=build_seed_publish_dag(
                spec=spec,
                prefixes=prefixes,
                publish_task_queue=plan.publish_task_queue,
            ),
        ),
        client=ae,
        minter=plan.minter,
    )
    payload = build_seed_submit_payload(
        spec=spec,
        run_id=plan.run_id,
        ae_workflow_slug=seeded.slug,
        app_service_url=plan.app_service_url,
    )
    retry = plan.submit_retry
    if retry is None:
        ae_run_id = await ae.submit_workflow(payload, slug=seeded.slug)
    else:
        ae_run_id = await ae.submit_workflow(
            payload,
            slug=seeded.slug,
            retries=retry.retries,
            retry_sleep_seconds=retry.sleep_seconds,
        )

    result = await ae.poll_native_status(
        ae_run_id,
        interval_seconds=plan.poll_interval_seconds,
        timeout_seconds=plan.poll_timeout_seconds,
        progress_stall_seconds=plan.progress_stall_seconds,
    )
    if not result.all_nodes_succeeded:
        raise SeedPublishFailedError(
            message=(
                f"the lineage-parent seed for {spec.qualified_name} did not "
                f"publish: AE status={result.status.value}. Nothing under test "
                "has run yet — the connector's own run would proceed against a "
                "connection whose entities and connection cache are absent or "
                f"partial. Seed run: slug={seeded.slug} run_id={ae_run_id}"
            ),
            resource=spec.qualified_name,
            actual_state=f"AE status={result.status.value}",
        )

    logger.info(
        "harness seed: published %d skeleton asset(s) under %s (%s) via slug=%s "
        "run_id=%s",
        report.total,
        spec.qualified_name,
        ", ".join(f"{name}={count}" for name, count in written.created.items())
        or "empty tree",
        seeded.slug,
        ae_run_id,
    )
    return SeededConnection(
        qualified_name=spec.qualified_name,
        created=written.created,
        prefix=prefixes.root,
        transformed_data_prefix=prefixes.transformed,
        ae_workflow_slug=seeded.slug,
        ae_run_id=ae_run_id,
    )
