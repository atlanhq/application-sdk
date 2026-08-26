"""Atlas reads, lifted out of ``testing/e2e/client.py``.

``client.py`` is 2,475 lines carrying two unrelated concerns: talking to the
Automation Engine, and searching Atlas. Splitting it here and in
:mod:`application_sdk.testing.harness.automation_engine` is child F on FND-224.

Two things change on the way across, and neither is cosmetic:

**Async only.** Five ``asyncio.run`` bridges survive in ``client.py``
(``_connection_search_async``, ``_search_counts_async``, ``_count_total_async``,
the sample paths) — each stands up a fresh event loop and a fresh
``AsyncAtlanClient``, and therefore a fresh TLS handshake, per call. The
``_async`` implementations already exist; what lifts here is the async twin, and
the sync entry points become one-liners over
:func:`~application_sdk.testing.harness.bridge.run_sync` (decision D1).

**No fail-open counts.** All four count and sample methods currently return zeros
or ``[]`` on a search error. The visible symptom is not a silent pass but a
*misleading diagnosis*: the run reports "asset floor not met" and points the
reader at the connector when the real fault was Atlas. These readers return
:class:`~application_sdk.testing.harness.outcome.Indeterminate` instead, so an
unreadable count can no longer be graded as a low count.

``packages/conformance``'s ``rules/client_seam.py`` names
``application_sdk/testing/e2e/client.py`` as *the* sanctioned low-level Atlan
path, so it needs updating when these reads move — child F, not this issue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from application_sdk.testing.harness._errors import HarnessNotBuiltError
from application_sdk.testing.harness.outcome import Outcome

__all__ = ["count_assets", "sample_qualified_names"]


async def count_assets(
    connection_qualified_name: str, type_names: Sequence[str]
) -> Outcome[Mapping[str, int]]:
    """Count assets of each named type under a connection.

    Args:
        connection_qualified_name: Connection prefix to count under.
        type_names: Atlan type names to count. Empty counts nothing and returns
            an empty mapping — not the total.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying type
        name -> count, or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        search could not be read. A type with no assets is a ``0`` in a settled
        result; it is never the same value as an unreadable search.

    Raises:
        HarnessNotBuiltError: Always — implementation is child F on FND-224.
    """
    raise HarnessNotBuiltError(
        message="count_assets is not implemented yet",
        operation="count_assets",
        reason="child F on FND-224",
        issue="FND-224",
        component="harness_atlas",
    )


async def sample_qualified_names(
    connection_qualified_name: str,
    type_names: Sequence[str],
    *,
    per_type: int,
) -> Outcome[Mapping[str, Sequence[str]]]:
    """Sample qualified names of each named type under a connection.

    Args:
        connection_qualified_name: Connection prefix to sample under.
        type_names: Atlan type names to sample.
        per_type: How many names to sample per type.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying type
        name -> sampled qualified names, or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        search could not be read.

    Raises:
        HarnessNotBuiltError: Always — implementation is child F on FND-224.
    """
    raise HarnessNotBuiltError(
        message="sample_qualified_names is not implemented yet",
        operation="sample_qualified_names",
        reason="child F on FND-224",
        issue="FND-224",
        component="harness_atlas",
    )
