"""Turn a resolved spec into the transformed NDJSON a connector would have emitted.

This is the half of the seed that has to be byte-faithful to a *producer we do
not own*. The publish app reads a ``transformed_data_prefix`` of ``**/*.json``
NDJSON files, decodes each line through pyatlan_v9's ``from_atlas_json``, and
builds both the Atlas entities and the connection-cache SQLite from that one
input. So the seed emits exactly what a crawler emits and nothing more:

* **pyatlan_v9**, not ``pyatlan.model.assets``. The validator
  (:mod:`application_sdk.validation.assets`) and publish both decode v9, and the
  two model trees disagree about attribute names in enough places that a v1
  payload validates locally and then loses attributes in publish.
* **``to_atlas_format``**, not ``model_dump``. It flattens relationship
  references into ``attributes`` — where connector ``serialize_entity``
  implementations put them on purpose, and where ``from_atlas_json`` looks.
* **No ``guid``.** pyatlan_v9's ``creator`` constructors stamp a negative
  placeholder guid for the bulk-save API; transformed output carries none (see
  ``tests/unit/transformers/atlas/resources/transformed_*.json``), and publish
  assigns its own.

The qualified names come from the ``creator`` constructors rather than from
string concatenation here, for the same reason the spec is data: ``creator``
composes ``{parent_qn}/{name}`` with the segment byte for byte as given, which
is the composition rule the spec documents and the one Atlas resolves against.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.seed._spec import ResolvedSeedSpec

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan_v9 is a lazy import
    from pyatlan_v9.model.assets import Asset

logger = get_logger(__name__)

__all__ = [
    "TRANSFORMED_FILE_NAME",
    "WrittenSeed",
    "connection_entity",
    "ndjson_bytes",
    "skeleton_assets",
    "write_transformed_dir",
]

#: Single NDJSON file the seed writes. One file, not one per type: publish globs
#: ``**/*.json`` and orders by the type graph it derives from the records, so
#: splitting buys nothing and multiplies the ways a partial upload can look
#: complete.
TRANSFORMED_FILE_NAME = "assets.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class WrittenSeed:
    """The local transformed directory a seed produced, before it is uploaded.

    Attributes:
        directory: The ``transformed`` directory itself — what
            :func:`~application_sdk.validation.assets.validate_transformed_dir`
            walks and what the upload mirrors.
        path: The single NDJSON file inside it.
        created: Type name -> count of records written.
    """

    directory: Path
    path: Path
    created: dict[str, int]


def skeleton_assets(spec: ResolvedSeedSpec) -> list[Asset]:
    """Build the skeleton entities for *spec*, parents strictly before children.

    The ordering is load-bearing: publish derives its write order from the type
    graph, but the referential-integrity pre-check reads the batch as a stream,
    and a reader that meets a child before its parent has to buffer the whole
    batch to say whether the parent was ever emitted. Emitting in tree order
    keeps that check linear and keeps the file readable by a human diffing it
    against a connector's goldens.

    Args:
        spec: The resolved spec. Its segments are already validated, so every
            qualified name composed here is the one the spec declares.

    Returns:
        Database, Schema, Table/View and Column instances in tree order. The
        Connection is deliberately absent — publish creates it from
        :func:`connection_entity`, and emitting it here as well would have the
        run's own connection processed twice.
    """
    from pyatlan_v9.model.assets import (  # noqa: PLC0415
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


def connection_entity(spec: ResolvedSeedSpec) -> dict[str, Any]:
    """Build the Connection entity publish creates the seeded connection from.

    Handed to the publish node as ``connection_entity`` alongside
    ``connection_creation_enabled``, which is how ``ConnectionProcessor`` reaches
    search-then-create-then-wait-for-policy-sync. That last step is why seeding
    through publish needs no policy-window retry of its own: the wait a fresh
    connection requires before it accepts child writes is inside the app that
    owns the connection, not bolted onto the harness.

    Args:
        spec: The resolved spec — its ACL and identity, verbatim.

    Returns:
        The ``{"typeName": "Connection", "attributes": {...}}`` dict.
    """
    from pyatlan_v9.model.enums import AtlanConnectorType  # noqa: PLC0415

    connector = AtlanConnectorType(spec.connector_type)
    return {
        "typeName": "Connection",
        "attributes": {
            "name": spec.display_name,
            "qualifiedName": spec.qualified_name,
            "connectorName": connector.value,
            "category": connector.category.value,
            "adminUsers": list(spec.admin_users),
            "adminGroups": list(spec.admin_groups),
            "adminRoles": list(spec.admin_roles),
        },
    }


def ndjson_bytes(assets: Sequence[Asset]) -> bytes:
    """Serialise *assets* as one NDJSON payload in transformed-output shape.

    Args:
        assets: Skeleton assets, in the order they should appear.

    Returns:
        Newline-delimited JSON, one record per asset, with a trailing newline —
        the shape ``iter_ndjson_lines`` and publish's own reader both accept.
    """
    return b"".join(orjson.dumps(record) + b"\n" for record in _records(assets))


def _records(assets: Sequence[Asset]) -> Iterator[dict[str, Any]]:
    """Yield the Atlas-format dict for each asset, guid stripped.

    Args:
        assets: Skeleton assets.

    Yields:
        One transformed-output record per asset.
    """
    from pyatlan_v9.model.transform import to_atlas_format  # noqa: PLC0415

    for asset in assets:
        record = to_atlas_format(asset)
        record.pop("guid", None)
        yield record


def write_transformed_dir(spec: ResolvedSeedSpec, root: Path) -> WrittenSeed:
    """Write *spec*'s NDJSON under ``root/transformed`` and count what landed.

    Written locally first, deliberately: the offline validation pass wants a
    directory to walk, and running it before the upload means a batch that would
    not survive publish never reaches the tenant at all.

    Args:
        spec: The resolved spec to serialise.
        root: Local directory to build the transformed tree under. Created if
            absent.

    Returns:
        The :class:`WrittenSeed` naming the directory, the file and the per-type
        counts.
    """
    assets = skeleton_assets(spec)
    directory = root / "transformed"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TRANSFORMED_FILE_NAME
    path.write_bytes(ndjson_bytes(assets))

    created: dict[str, int] = {}
    for asset in assets:
        type_name = getattr(asset, "type_name", None) or type(asset).__name__
        created[type_name] = created.get(type_name, 0) + 1
    logger.info(
        "harness seed: serialised %d skeleton asset(s) for %s to %s (%s)",
        len(assets),
        spec.qualified_name,
        path,
        ", ".join(f"{name}={count}" for name, count in created.items()) or "empty tree",
    )
    return WrittenSeed(directory=directory, path=path, created=created)
