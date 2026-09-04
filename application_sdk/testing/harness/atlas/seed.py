"""Declarative lineage-parent seeding: skeleton SQL assets a run's refs resolve to.

Why this exists (FND-402): a lineage-only connector publishes Process /
ColumnProcess entities whose ``inputs``/``outputs`` reference *another source's*
assets by qualified name — Snowflake tables under a Coalesce run, warehouse
tables under an ADF or Mode run. On a connector-scoped e2e tenant that other
source has never been crawled, so Atlas rejects every reference
(``ATLAS-404-00-00A``) and the publish fails wholesale: 72 entities on adf, 9 on
mode, 19,210 on coalesce (run 33024618223) — the largest single failure class in
the fleet's e2e rollout.

The fix is not a crawl of the referenced source. Atlas resolves a lineage ref by
**exact-match qualified name and exact type** — the resolver never fuzzy-matches
a segment, never case-folds, and never accepts a ``Table`` where the ref said
``View`` — so a *skeleton* entity carrying nothing but ``name``,
``qualifiedName`` and its parent linkage satisfies it completely. The coalesce
pilot proved that end to end: a seeded skeleton tree took the aws leg's publish
from 19,210 ``ATLAS-404`` failures to zero.

That exactness cuts both ways, and it is the one thing a seeding suite must get
right: **every QN segment in the spec must match what the connector under test
emits byte for byte, case included.** A connector that upper-cases segments
(Snowflake refs are ``.upper()``-d) must be seeded upper-cased; a connector
whose warehouse QNs are not config-pinnable must precompute them from its own
source fixture rather than invent them. The pilot derives its spec from the
connector's committed transform goldens for precisely this reason — QN parity by
construction.

The seeded tree hangs under its **own** ephemeral Connection, minted the same
way the run's is, so the one teardown mechanism the harness already has —
:func:`~application_sdk.testing.harness.teardown.purge_connection` — removes it
whole. Nothing here reaches for a long-lived shared connection: the assets are
this run's to create and this run's to delete.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.atlas import create_connection, poll_for_connection
from application_sdk.testing.harness.atlas._errors import (
    SeedConnectionNotSearchableError,
    SeedTreeNotWritableError,
)
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import Settled
from application_sdk.testing.harness.waiting import poll_until

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan is a lazy import
    from pyatlan.client.aio.client import AsyncAtlanClient
    from pyatlan.model.assets import Asset

logger = get_logger(__name__)

__all__ = [
    "DatabaseSpec",
    "SchemaSpec",
    "SeedSpec",
    "SeededConnection",
    "TableSpec",
    "seed_assets",
]

#: Assets per ``asset.save``. A bulk save is bounded by request size rather than
#: by the URL ceiling that bounds the purge's DELETE, but the same second reason
#: applies: a failing chunk orphans less, and the first chunk doubles as the
#: policy-window probe, which wants to be small enough to retry cheaply.
SEED_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class TableSpec:
    """One table-level asset, typed strictly as ``Table`` or ``View``.

    ``type_name`` is a field rather than two sibling classes because the two
    differ in nothing but the type — and the type is the half of the exactness
    contract a spec author is most likely to get wrong: a ref that says ``View``
    is **not** resolved by a ``Table`` at the same qualified name, so a
    connector that emits view refs must be seeded with views.

    Attributes:
        name: The table segment, byte for byte as the connector emits it.
        type_name: The exact Atlan type the connector's refs name.
        columns: Column segments, in the order the connector emits them —
            ``order`` on the seeded Column is its 1-based position here.
    """

    name: str
    type_name: Literal["Table", "View"] = "Table"
    columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SchemaSpec:
    """One schema and the tables/views under it."""

    name: str
    tables: tuple[TableSpec, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseSpec:
    """One database and the schemas under it."""

    name: str
    schemas: tuple[SchemaSpec, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SeedSpec:
    """Everything :func:`seed_assets` needs, declared rather than performed.

    Declarative on purpose: the spec is data a unit test can pin — the exact
    QNs and type names it implies — without a tenant, which is how QN parity
    with the connector's own emissions stays asserted rather than hoped.

    Attributes:
        connector_type: Atlan catalog type segment of the *referenced* source
            (e.g. ``"snowflake"`` for a Coalesce run's warehouse refs) — not the
            connector under test.
        qualified_name: The Connection QN to seed under, minted per run the same
            way the suite's own is (see
            :meth:`~application_sdk.testing.harness.identity.Minter.connection_identity`).
            Every ref the connector emits must be rebased onto this prefix.
        display_name: Human-facing name on the seeded Connection.
        admin_users: Usernames on the Connection's admin ACL.
        admin_groups: Group aliases on the admin ACL.
        admin_roles: Role GUIDs on the admin ACL.
        databases: The skeleton tree. QNs are composed as
            ``{qualified_name}/{db}/{schema}/{table}[/{column}]`` — segments
            exactly as given, because Atlas ref resolution is exact-match and
            type-strict (see the module docstring).
    """

    connector_type: str
    qualified_name: str
    display_name: str
    admin_users: tuple[str, ...] = ()
    admin_groups: tuple[str, ...] = ()
    admin_roles: tuple[str, ...] = ()
    databases: tuple[DatabaseSpec, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededConnection:
    """What one seeding pass created, for the report and the teardown registry.

    Attributes:
        qualified_name: The seeded Connection's QN — what a caller registers for
            teardown and rebases the connector's refs onto.
        created: Type name -> count of skeleton entities written, the Connection
            excluded. Counts, not names: the names are the spec, verbatim.
    """

    qualified_name: str
    created: Mapping[str, int] = field(default_factory=dict)


def _skeleton_assets(spec: SeedSpec) -> list[Asset]:
    """Build the skeleton entities for *spec*, parents strictly before children.

    The ordering is load-bearing twice over: Atlas rejects a child whose parent
    ref does not resolve, and the batching in :func:`seed_assets` cuts this list
    into consecutive chunks — a child may share its parent's batch but must
    never precede it.

    pyatlan's ``creator`` constructors are used deliberately: they derive each
    qualified name as ``{parent_qn}/{name}`` with the segment byte for byte as
    given, which is exactly the composition rule the spec documents, and they
    set precisely the skeleton attributes (name, qualifiedName, parent linkage)
    and nothing more.
    """
    from pyatlan.model.assets import (  # noqa: PLC0415
        Column,
        Database,
        Schema,
        Table,
        View,
    )

    assets: list[Asset] = []
    for database in spec.databases:
        database_qn = f"{spec.qualified_name}/{database.name}"
        assets.append(
            Database.creator(
                name=database.name, connection_qualified_name=spec.qualified_name
            )
        )
        for schema in database.schemas:
            schema_qn = f"{database_qn}/{schema.name}"
            assets.append(
                Schema.creator(name=schema.name, database_qualified_name=database_qn)
            )
            for table in schema.tables:
                parent_type = Table if table.type_name == "Table" else View
                assets.append(
                    parent_type.creator(
                        name=table.name, schema_qualified_name=schema_qn
                    )
                )
                table_qn = f"{schema_qn}/{table.name}"
                for order, column in enumerate(table.columns, start=1):
                    assets.append(
                        Column.creator(
                            name=column,
                            parent_type=parent_type,
                            parent_qualified_name=table_qn,
                            order=order,
                        )
                    )
    return assets


@dataclass(frozen=True, slots=True, kw_only=True)
class _SeedWriteReading:
    """One attempt at the first child-write batch under a fresh Connection.

    The same shape :class:`~application_sdk.testing.e2e.base._SeedProbeReading`
    carries, for the same reason: while a fresh Connection's access policies are
    still provisioning, a refused write is the *expected* answer, not a failed
    read — modelling it as a probe error would spend the wait's transient streak
    on the normal case and end the loop long before the policies could go live.

    Attributes:
        error: What the write raised, or ``None`` when it succeeded. Retained
            because it is what the caller re-raises when the wait never settles.
    """

    error: Exception | None = None

    @property
    def permitted(self) -> bool:
        """Whether the write went through."""
        return self.error is None


async def seed_assets(
    client: AsyncAtlanClient,
    spec: SeedSpec,
    *,
    connection_budget: Budget,
    probe_budget: Budget,
) -> SeededConnection:
    """Create the Connection and the skeleton tree *spec* declares, and wait.

    Four steps, each one the harness already knows how to perform for the run's
    own connection — this composes them for a *second*, referenced-source
    connection:

    1. Create the Connection at ``spec.qualified_name`` via
       :func:`~application_sdk.testing.harness.atlas.create_connection` — the
       name is an input, never derived, so two matrix legs cannot collide.
    2. Poll until it is searchable, under *connection_budget*.
    3. Write the skeleton tree in :data:`SEED_BATCH_SIZE` chunks, parents before
       children. The **first** chunk doubles as the policy-window probe: a fresh
       Connection 403s child writes until its access policies go live, no API
       reports when that has happened, and a successful child write is the only
       signal — so the first chunk is retried under *probe_budget* while every
       later chunk is written once.
    4. Report what was created, per type, so the caller can log it and register
       the QN for teardown.

    Args:
        client: An open client from
            :func:`~application_sdk.testing.harness.atlas.atlas_client`, held
            for the whole seed — a large tree is many round trips.
        spec: What to seed. See :class:`SeedSpec` for the exactness contract.
        connection_budget: Allowance for the Connection becoming searchable.
        probe_budget: Allowance for the first child write being permitted.

    Returns:
        A :class:`SeededConnection` naming the QN and counting what was written.

    Raises:
        UnknownConnectorTypeError: ``spec.connector_type`` is not a pyatlan
            ``AtlanConnectorType``.
        SeedConnectionNotSearchableError: Atlas never returned the Connection
            within *connection_budget*.
        SeedTreeNotWritableError: The first-write wait ran to no verdict at all.
        Exception: Whatever the first write last raised when it never became
            permitted within *probe_budget*, or whatever pyatlan raises when a
            later batch is rejected. Not collapsed into a report: unlike the
            purge, a failed seed leaves the run with nothing to resolve against,
            and there is no degraded mode worth continuing into.
    """
    await create_connection(
        client,
        qualified_name=spec.qualified_name,
        display_name=spec.display_name,
        connector_type=spec.connector_type,
        admin_users=spec.admin_users,
        admin_groups=spec.admin_groups,
        admin_roles=spec.admin_roles,
    )

    searchable = await poll_for_connection(
        client, spec.qualified_name, budget=connection_budget
    )
    if not (isinstance(searchable, Settled) and searchable.value):
        raise SeedConnectionNotSearchableError(
            message=(
                f"Seeded lineage-parent connection {spec.qualified_name} never "
                f"became searchable ({type(searchable).__name__}). Nothing can "
                "be written beneath a connection Atlas cannot see."
            ),
            resource=spec.qualified_name,
            actual_state=f"not returned by Atlas search ({searchable.label})",
            cause=getattr(searchable, "cause", None),
        )

    assets = _skeleton_assets(spec)
    batches = [
        assets[offset : offset + SEED_BATCH_SIZE]
        for offset in range(0, len(assets), SEED_BATCH_SIZE)
    ]
    started = time.monotonic()
    if batches:
        await _retry_first_batch(client, spec, batches[0], probe_budget=probe_budget)
        for batch in batches[1:]:
            await client.asset.save(batch)

    created: dict[str, int] = {}
    for asset in assets:
        created[asset.type_name] = created.get(asset.type_name, 0) + 1
    logger.info(
        "harness seed: wrote %d skeleton asset(s) under %s in %.1fs (%s)",
        len(assets),
        spec.qualified_name,
        time.monotonic() - started,
        ", ".join(f"{type_name}={count}" for type_name, count in created.items())
        or "empty tree",
    )
    return SeededConnection(qualified_name=spec.qualified_name, created=created)


async def _retry_first_batch(
    client: AsyncAtlanClient,
    spec: SeedSpec,
    batch: list[Asset],
    *,
    probe_budget: Budget,
) -> None:
    """Retry the first child-write batch until the connection permits it.

    Args:
        client: The open client the whole seed holds.
        spec: The spec being seeded, for the log lines and error text.
        batch: The first :data:`SEED_BATCH_SIZE` skeleton assets.
        probe_budget: Allowance for the write becoming permitted.

    Raises:
        Exception: Whatever the write last raised, when it never succeeded
            within *probe_budget* — the suite has to see the 403 itself, not a
            harness paraphrase of it.
        SeedTreeNotWritableError: The wait ran to no verdict at all.
    """

    async def _attempt() -> _SeedWriteReading:
        try:
            await client.asset.save(batch)
        # conformance: ignore[E004] the write goes through pyatlan and can raise anything; narrowing it would let one tenant's rejection type escape the retry the loop exists to perform
        except Exception as error:
            # conformance: ignore[E007] the refusal IS the reading — carried out as _SeedWriteReading(error=...) and re-raised by the caller when the wait never settles, so nothing is hidden; a fresh connection 403s child writes until its policies are live, which is the normal case here
            return _SeedWriteReading(error=error)
        return _SeedWriteReading()

    outcome = await poll_until(
        _attempt,
        settled=lambda reading: reading.permitted,
        budget=probe_budget,
        label=f"a permitted skeleton write under {spec.qualified_name}",
    )
    if isinstance(outcome, Settled):
        logger.info(
            "harness seed: first skeleton batch under %s written after %d "
            "attempt(s) — connection policies are live",
            spec.qualified_name,
            outcome.attempts,
        )
        return
    last = getattr(outcome, "last", None)
    logger.error(
        "harness seed: first skeleton batch under %s still refused after %d "
        "attempt(s) — a fresh connection 403s child writes until its access "
        "policies go live, so this means provisioning never completed",
        spec.qualified_name,
        outcome.attempts,
    )
    if last is not None and last.error is not None:
        raise last.error
    raise SeedTreeNotWritableError(
        message=(
            f"The first skeleton write under {spec.qualified_name} never ran to "
            f"a verdict ({type(outcome).__name__}), so whether the connection's "
            "policies went live is unknown."
        ),
        resource=spec.qualified_name,
        actual_state="seed write reached no verdict",
        cause=getattr(outcome, "cause", None),
    )
