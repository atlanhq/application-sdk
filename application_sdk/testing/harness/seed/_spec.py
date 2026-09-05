"""What to seed, declared rather than performed.

The spec is plain data on purpose: the qualified names and type names it implies
are the whole contract with the connector under test, and data is the only shape
a unit test can pin *without a tenant*. Everything that needs a tenant —
uploading the NDJSON, submitting the publish node — takes a
:class:`ResolvedSeedSpec` this module produced and validated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from application_sdk.testing.harness.atlas._errors import UnknownConnectorTypeError
from application_sdk.testing.harness.seed._errors import SeedSegmentInvalidError

__all__ = [
    "DatabaseSpec",
    "ResolvedSeedSpec",
    "SchemaSpec",
    "SeedSpec",
    "SeededConnection",
    "TableSpec",
]

#: Minimum slash-separated segments in a Connection qualified name
#: (``default/<connector>/<suffix>``). ``atlan-publish-app``'s own config
#: validation refuses ``connection_creation_enabled`` on anything that is not a
#: slash-delimited path, so a malformed QN here fails the seed's publish run
#: minutes later rather than at declaration.
_CONNECTION_QN_SEGMENTS = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class TableSpec:
    """One table-level asset, typed strictly as ``Table`` or ``View``.

    ``type_name`` is a field rather than two sibling classes because the two
    differ in nothing but the type — and the type is the half of the exactness
    contract a spec author is most likely to get wrong: a ref that says ``View``
    is **not** resolved by a ``Table`` at the same qualified name, so a connector
    that emits view refs must be seeded with views.

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
    """Everything the seed needs, as a suite *declares* it.

    ``qualified_name`` and ``display_name`` are ``None`` — not ``""`` — when the
    suite wants this run's minter to name the connection. ``None`` says "not
    declared"; an empty string would be a required value whose empty case means
    something else, which is the shape that lets a genuinely blank name pass for
    a deliberate one.

    Attributes:
        connector_type: Atlan catalog type segment of the *referenced* source
            (e.g. ``"snowflake"`` for a Coalesce run's warehouse refs) — not the
            connector under test.
        qualified_name: The Connection QN to seed under, or ``None`` to have the
            run's minter name it the same way it named the run's own. Every ref
            the connector emits must be rebased onto whichever it resolves to.
        display_name: Human-facing name on the seeded Connection, or ``None`` to
            follow the minted identity.
        admin_users: Usernames on the Connection's admin ACL.
        admin_groups: Group aliases on the admin ACL.
        admin_roles: Role GUIDs on the admin ACL.
        databases: The skeleton tree. QNs are composed as
            ``{qualified_name}/{db}/{schema}/{table}[/{column}]`` — segments
            exactly as given, because Atlas ref resolution is exact-match and
            type-strict (see :mod:`application_sdk.testing.harness.seed`).
    """

    connector_type: str
    qualified_name: str | None = None
    display_name: str | None = None
    admin_users: tuple[str, ...] = ()
    admin_groups: tuple[str, ...] = ()
    admin_roles: tuple[str, ...] = ()
    databases: tuple[DatabaseSpec, ...] = ()

    def resolve(self, *, qualified_name: str, display_name: str) -> ResolvedSeedSpec:
        """Settle the identity and validate every segment.

        Args:
            qualified_name: The Connection QN this seed lands on — the spec's
                own when it declared one, else the minted one.
            display_name: The Connection's display name, resolved the same way.

        Returns:
            The spec with both identity fields settled to ``str``, so nothing
            downstream re-derives them or re-checks for ``None``.

        Raises:
            UnknownConnectorTypeError: ``connector_type`` is not a pyatlan_v9
                ``AtlanConnectorType``.
            SeedSegmentInvalidError: Any segment — or the connection QN itself —
                cannot compose the qualified name the spec claims.
        """
        resolved = ResolvedSeedSpec(
            connector_type=self.connector_type,
            qualified_name=qualified_name,
            display_name=display_name,
            admin_users=self.admin_users,
            admin_groups=self.admin_groups,
            admin_roles=self.admin_roles,
            databases=self.databases,
        )
        validate_resolved_spec(resolved)
        return resolved


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedSeedSpec:
    """A :class:`SeedSpec` whose identity is settled and whose segments passed.

    Constructed only through :meth:`SeedSpec.resolve`, so holding one is the
    evidence that :func:`validate_resolved_spec` ran. Everything below the
    harness boundary — the serializer, the publish node builder — takes this
    type rather than ``SeedSpec`` for exactly that reason.
    """

    connector_type: str
    qualified_name: str
    display_name: str
    admin_users: tuple[str, ...] = ()
    admin_groups: tuple[str, ...] = ()
    admin_roles: tuple[str, ...] = ()
    databases: tuple[DatabaseSpec, ...] = ()

    def with_admins(
        self,
        *,
        admin_users: tuple[str, ...],
        admin_groups: tuple[str, ...],
        admin_roles: tuple[str, ...],
    ) -> ResolvedSeedSpec:
        """Return a copy carrying a different admin ACL.

        The ACL is the one part of a spec a suite routinely leaves to the
        harness — ``BaseE2ETest`` already resolved an admin identity for the
        run's own connection, and the seeded one wants the same. Kept off
        :meth:`SeedSpec.resolve` because it is a default, not an identity: a
        spec that declares its own ACL must keep it.
        """
        return replace(
            self,
            admin_users=admin_users,
            admin_groups=admin_groups,
            admin_roles=admin_roles,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededConnection:
    """What one seeding pass created, for the report and the teardown registry.

    Attributes:
        qualified_name: The seeded Connection's QN — what a caller registers for
            teardown and rebases the connector's refs onto.
        created: Type name -> count of skeleton entities emitted, the Connection
            excluded. Counts, not names: the names are the spec, verbatim.
        prefix: Object-store prefix root this seed wrote under, so teardown can
            delete it and a failure report can name it.
        transformed_data_prefix: The prefix handed to the publish node.
        ae_workflow_slug: Slug of the AE workflow the seed's publish ran under.
        ae_run_id: That run's id — the one link that shows what publish did.
    """

    qualified_name: str
    created: Mapping[str, int] = field(default_factory=dict)
    prefix: str = ""
    transformed_data_prefix: str = ""
    ae_workflow_slug: str = ""
    ae_run_id: str = ""


def validate_resolved_spec(spec: ResolvedSeedSpec) -> None:
    """Reject a spec that cannot compose the qualified names it declares.

    Args:
        spec: The resolved spec to check.

    Raises:
        UnknownConnectorTypeError: ``connector_type`` is not a pyatlan_v9
            ``AtlanConnectorType`` — publish would create a Connection with a
            ``connectorName`` no consumer recognises.
        SeedSegmentInvalidError: The connection QN is not a slash-delimited path
            of at least three non-empty segments, or any tree segment is empty,
            padded with whitespace, or carries a ``/``.
    """
    from pyatlan_v9.model.enums import AtlanConnectorType  # noqa: PLC0415

    try:
        AtlanConnectorType(spec.connector_type)
    except ValueError as error:
        raise UnknownConnectorTypeError(
            message=(
                f"cannot seed a lineage parent because {spec.connector_type!r} is "
                "not a pyatlan AtlanConnectorType. Pass the Atlan catalog type "
                "segment of the REFERENCED source (e.g. 'snowflake' for a "
                "Coalesce run's warehouse refs), not the connector under test"
            ),
            value_summary=spec.connector_type,
        ) from error

    segments = spec.qualified_name.split("/")
    if len(segments) < _CONNECTION_QN_SEGMENTS or not all(segments):
        raise SeedSegmentInvalidError(
            message=(
                f"the seeded connection qualified name {spec.qualified_name!r} is "
                f"not a slash-delimited path of at least {_CONNECTION_QN_SEGMENTS} "
                "non-empty segments (shape: 'default/<connector>/<suffix>'). "
                "atlan-publish-app refuses connection creation on anything else"
            ),
            field="qualified_name",
            value_summary=spec.qualified_name,
        )

    for database in spec.databases:
        _check_segment(database.name, field="databases[].name")
        for schema in database.schemas:
            _check_segment(schema.name, field="schemas[].name")
            for table in schema.tables:
                _check_segment(table.name, field="tables[].name")
                for column in table.columns:
                    _check_segment(column, field="columns[]")


def _check_segment(value: str, *, field: str) -> None:
    """Raise when *value* cannot be one segment of a qualified name.

    Args:
        value: The candidate segment.
        field: Dotted path of the spec field it came from, for the error.

    Raises:
        SeedSegmentInvalidError: *value* is empty, is padded with whitespace, or
            contains a ``/``.
    """
    if value and value == value.strip() and "/" not in value:
        return
    raise SeedSegmentInvalidError(
        message=(
            f"{field}={value!r} cannot be one segment of a qualified name: a "
            "segment must be non-empty, unpadded, and free of '/'. Seeding is "
            "exact-match — a segment that does not compose cleanly yields a QN "
            "no ref will ever resolve to"
        ),
        field=field,
        value_summary=value,
    )
