"""Typed leaves for lineage-parent seeding.

Separate from :mod:`application_sdk.testing.harness.atlas._errors` because
seeding no longer writes to Atlas at all: it writes transformed NDJSON to the
object store and submits a ``PublishWorkflow`` node, so its failure modes are a
missing store binding, a batch that would not survive publish, and a publish run
that did not succeed — none of which is an Atlas write.

Every leaf here is an ``InvalidInputError`` or a ``PreconditionError`` rather
than a ``DependencyUnavailableError``: a seed that fails leaves the run with
nothing for its refs to resolve against, and re-running the same call without
changing something first cannot succeed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError, PreconditionError

__all__ = [
    "SeedPublishEmptyError",
    "SeedPublishFailedError",
    "SeedSegmentInvalidError",
    "SeedStoreUnavailableError",
    "SeedTreeInvalidError",
]


@dataclass(kw_only=True)
class SeedSegmentInvalidError(InvalidInputError):
    """A spec segment cannot compose the qualified name it claims to.

    The whole value of a seed spec is that its qualified names match, byte for
    byte, what the connector under test emits. A segment that is empty, carries
    a ``/``, or is padded with whitespace silently composes a *different* QN
    from the one the spec appears to declare — a doubled separator, an extra
    level of nesting, or a trailing space Atlas will never match. Caught at
    declaration time because the alternative is a green publish that resolves
    nothing.
    """

    code: ClassVar[str] = "INVALID_INPUT_SEED_SEGMENT"
    field: str | None = "name"


@dataclass(kw_only=True)
class SeedStoreUnavailableError(PreconditionError):
    """No object store to write the seed's transformed NDJSON into.

    Synthetic-publish seeding hands the tenant's ``publish`` app a prefix, so
    the harness needs write access to the store that app reads. In CI that is
    the configurator-emitted ``atlan-objectstore`` Dapr component the sdr-e2e
    action selects into ``ci-deploy/components``; absent it there is nothing to
    point publish at, and the seed cannot start.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_STORE_UNAVAILABLE"
    expected_state: str | None = (
        "an object-store binding the tenant's publish app reads"
    )


@dataclass(kw_only=True)
class SeedTreeInvalidError(PreconditionError):
    """The seed's own NDJSON would not survive publish, so it is not submitted.

    Raised by the offline pre-submit check
    (:func:`~application_sdk.validation.assets.validate_transformed_dir` with
    referential integrity on). Tenant-free and cheap, and it runs *before* the
    upload: a batch with a dangling parent, or an asset that fails pyatlan_v9's
    own ``validate()``, publishes partially and leaves the connector's refs
    resolving against a tree missing exactly the levels the check names.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_TREE_INVALID"
    expected_state: str | None = "every seeded asset valid and every parent present"


@dataclass(kw_only=True)
class SeedPublishFailedError(PreconditionError):
    """The seed's ``PublishWorkflow`` run did not succeed on every node.

    Distinct from the connector's own DAG failing: nothing the suite is testing
    has run yet. The run under test would proceed against a connection whose
    entities — and whose connection cache — are absent or partial, and would
    then fail as an ``ATLAS-404`` cascade that names the connector rather than
    the seed.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_PUBLISH_FAILED"
    expected_state: str | None = "the seed's publish run succeeded on every node"


@dataclass(kw_only=True)
class SeedPublishEmptyError(PreconditionError):
    """The seed's publish run succeeded and landed nothing in Atlas.

    The failure mode a node-status check cannot see. Publish is handed a
    ``transformed_data_prefix``; a prefix it cannot read is an *empty batch*, not
    an error, so the node reports success having published zero entities. That
    reads as a clean seed and then surfaces minutes later as the connector's own
    ``ATLAS-404`` cascade — naming the connector, in a different repo, for a
    problem in the seed's object-store wiring.

    The likely causes, in the order worth checking: the tenant's publish service
    account cannot read the seed's prefix; the harness wrote to a different
    bucket than the one publish reads (``seed_object_store`` resolved the
    connector's deployment store rather than the tenant blobstorage binding); or
    the spec declared an empty tree.
    """

    code: ClassVar[str] = "PRECONDITION_SEED_PUBLISH_EMPTY"
    expected_state: str | None = "at least one seeded asset present in Atlas"
