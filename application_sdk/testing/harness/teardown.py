"""Purging what a harness run created.

Lifted from ``BaseE2ETest.teardown_method``, whose purge path is the part of the
harness with the least test coverage and the most consequence: assets a failed
run leaves behind accumulate in a shared tenant, and a leftover half-set-up
connection is exactly what greens a later run that should have failed.

Two mechanics have to survive the lift, both non-obvious:

**Batching is a hard requirement, not a tuning knob.** ``pyatlan``'s
``purge_by_guid`` puts one ``guid=`` query parameter per asset into a single
DELETE, and ``httpx`` refuses to build a URL whose query exceeds its
``MAX_URL_LENGTH``. At roughly 42 bytes per GUID that is a ceiling near 1,550
assets, raised client-side — so an unbatched purge of a normal crawl's output
deletes *nothing*. The batch stays well under the ceiling for a second reason:
purge is expensive server-side, and a smaller batch means a failing chunk
orphans less.

**A failed purge is reported, never raised.** Teardown runs after the assertions
have already decided the verdict. Raising here replaces a real failure with a
cleanup error and loses the diagnosis.

Implementation is child G on FND-224.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = ["PURGE_BATCH_SIZE", "PurgeReport", "purge_connection"]

#: Assets per DELETE. See the module docstring for why this is a correctness
#: bound rather than a performance choice.
PURGE_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True, kw_only=True)
class PurgeReport:
    """What a purge managed to delete, and what it did not.

    Attributes:
        purged: Count of assets successfully deleted.
        orphaned: Qualified names of assets the purge could not delete. Named
            individually rather than counted: an operator cleaning up by hand
            needs the list, and a count tells them only that they have a problem.
        errors: One line per failed batch, in order.
    """

    purged: int = 0
    orphaned: Sequence[str] = field(default_factory=tuple)
    errors: Sequence[str] = field(default_factory=tuple)


async def purge_connection(connection_qualified_name: str) -> PurgeReport:
    """Delete every asset under *connection_qualified_name*, then the connection.

    Args:
        connection_qualified_name: The ephemeral connection this run created.
            Must be a name the run minted itself — never a long-lived shared
            connection, whose assets are not this run's to delete.

    Returns:
        A :class:`PurgeReport`. Failures are reported here, not raised: see the
        module docstring.

    Raises:
        NotImplementedError: Always — implementation is child G on FND-224.
    """
    raise NotImplementedError("purge_connection is child G on FND-224")
