"""Offload-cancel rule definitions (P064–P065, FND-4707).

A cancel stops only the coroutine awaiting ``run_in_thread``; Python cannot
kill the thread, so the offloaded work runs on with its memory and CPU while
the worker starts its next activity.  In a production RCA a cancelled offload
still held ~6.4 GiB when the retry landed on the same pod, and the pod was
OOM-killed.  From the SDK release in ``suite.checks.offload_cancel`` every
``run_in_thread`` call carries a ``CancelHandle`` the thread can reach and the
SDK's own offloads stop on cancel; these rules flag the two app shapes the SDK
cannot reach on its own:

* ``P064`` UncancellableOffloadedCursorStatement — a ``run_in_thread`` call
  runs a DB-API cursor statement (``<cursor>.execute`` / ``executemany`` on a
  cursor from ``<conn>.cursor()``) and registers no driver cancel, so a
  cancelled query keeps running on the source.
* ``P065`` BatchedResultsIteratedOnLoop — inside an ``async def``, the result of
  ``await <client>.get_batched_results(...)`` is iterated with a sync ``for``,
  so every chunk is read and built on the event loop.

Why only these two
------------------
Both are exact syntax matches within one module.  The broader shape — a long
loop inside ``run_in_thread`` with no ``is_cancelled()`` check — needs a
judgement about what "long" means and is deliberately not a rule.

SDK-version gate
----------------
Both fixes use SDK surfaces that do not exist before the floor, so the check is
silent unless the app's ``uv.lock`` resolves ``atlan-application-sdk`` at or
above it.

Scope
-----
Both rules are ``app``-scoped: they describe consumer code, and the gate reads a
*consumed* SDK version, which the SDK repo does not have.  P-ids are a
permanent public contract (see ``prescriptions.py``).
"""

from __future__ import annotations

from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_APPS_DOC = "https://github.com/atlanhq/application-sdk/blob/main/docs/concepts/apps.md"

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P064",
        canonical_reference=(
            "application_sdk application_sdk/clients/sql.py — `BaseSQLClient.run_query` "
            "and `_execute_async_read_operation` offload every driver call through "
            "`run_in_thread` with a `CancelHandle` the cursor's cancel is registered on, "
            "so a cancelled caller stops the statement at the driver. No reference app "
            "offloads a raw DB-API cursor."
        ),
        scope=RuleScope.APP,
        name="UncancellableOffloadedCursorStatement",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="async-correctness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.56.0",
        rationale=(
            "A cancel stops only the coroutine awaiting run_in_thread; the thread and "
            "the query it sent run on, holding the source's compute, a connection and "
            "this worker's memory while the worker starts its next activity. A "
            "cancelled offload in a production RCA still held ~6.4 GiB when its retry "
            "landed on the same pod (FND-4707). The query stops only if the driver is "
            "told to cancel it."
        ),
        short_description=(
            "`run_in_thread` runs a DB-API cursor statement with no driver cancel "
            "registered"
        ),
        full_description=(
            "A ``run_in_thread`` call offloads work that sends a DB-API cursor\n"
            "statement — ``<cursor>.execute(...)`` or ``executemany(...)`` on a cursor\n"
            "bound from ``<conn>.cursor()`` in the offloaded function, or the bare\n"
            "``cursor.execute`` passed straight to ``run_in_thread`` — and nothing in\n"
            "the offloaded function registers a driver cancel.  When the activity is\n"
            "cancelled the await stops, but the thread keeps waiting on the query\n"
            "and the source keeps running it.\n"
            "\n"
            "Fix: register the driver's own cancel on the call's handle from inside\n"
            "the offloaded function, once the cursor exists::\n"
            "\n"
            "    from application_sdk.execution.heartbeat import current_cancel_handle\n"
            "\n"
            "    def _query(sql):\n"
            "        cursor = connection.cursor()\n"
            "        handle = current_cancel_handle()\n"
            "        if handle is not None:\n"
            "            handle.set(cursor.cancel)   # the driver's own cancel\n"
            "        cursor.execute(sql)\n"
            "        return cursor.fetchall()\n"
            "\n"
            "    rows = await self.run_in_thread(_query, sql)\n"
            "\n"
            "Not a mechanical rewrite: the cancel call is driver-specific —\n"
            "``cursor.cancel()`` (pyodbc), ``connection.cancel()`` (psycopg2),\n"
            "an abort keyed on the query id (Snowflake) — and some drivers have\n"
            "none.  Where the driver cannot cancel, give the statement a\n"
            "server-side timeout and suppress with a reason that says so.  See\n"
            + _APPS_DOC
            + ".\n"
            "\n"
            "Matching is exact and module-local: the offloaded function must be a\n"
            "``def`` or ``lambda`` in the same module (a nested ``def``, a\n"
            "module-level ``def`` or a method on ``self``), seen through\n"
            "``functools.partial``.  ``<handle>.bind(fn)`` counts as registered,\n"
            "and so does a ``.set(...)`` call in the function on\n"
            "``current_cancel_handle()`` or on a name bound from it; looking the\n"
            "handle up without setting it does not, and neither does a ``.set``\n"
            "inside a nested ``def`` or ``lambda``.  A cursor received as an\n"
            "argument, or a function from another module, is not followed.  The\n"
            "check is silent unless the app's ``uv.lock`` resolves an SDK that has\n"
            "``current_cancel_handle``.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P064]\n"
            "<reason>`` on the ``run_in_thread`` line records a deliberate exception\n"
            "and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p064",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P065",
        canonical_reference=(
            "application_sdk application_sdk/storage/formats/__init__.py — "
            "`Writer._write_batched_dataframe` reads any async iterable with "
            "`async for`, which is how `write_batches(await "
            "client.get_batched_results(query))` reads each chunk off the event loop."
        ),
        scope=RuleScope.APP,
        name="BatchedResultsIteratedOnLoop",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="async-correctness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.56.0",
        rationale=(
            "A sync `for` over get_batched_results() runs every fetchmany and every "
            "DataFrame build on the event loop, which stops the activity's "
            "auto-heartbeat and leaves a cancel unable to stop the read. `async for` "
            "reads each chunk with run_in_thread under a cancel handle (FND-4707)."
        ),
        short_description=(
            "`get_batched_results(...)` iterated with a sync `for` inside an "
            "`async def`"
        ),
        full_description=(
            "Inside an ``async def``, the result of ``await\n"
            "<client>.get_batched_results(...)`` is iterated with a sync ``for`` —\n"
            "directly, or through a local name bound once from it — or with a sync\n"
            "comprehension.  Each step of that loop fetches the next chunk from the\n"
            "cursor and builds its DataFrame on the event loop: the loop blocks, the\n"
            "activity's heartbeat stalls, and a cancel cannot stop the read.\n"
            "\n"
            "Fix: iterate with ``async for``, which reads each chunk on the SDK's\n"
            "blocking pool and stops the read when the activity is cancelled::\n"
            "\n"
            "    batches = await client.get_batched_results(query)\n"
            "    async for frame in batches:\n"
            "        ...\n"
            "\n"
            "A comprehension becomes ``[f async for f in batches]``.  The result is\n"
            "the same frames either way; only where they are read changes.  Handing\n"
            "the result to ``write_batches(...)`` is already correct and is not\n"
            "flagged.\n"
            "\n"
            "Not a mechanical rewrite: only a ``BaseSQLClient`` on a sync engine\n"
            "returns a result that supports ``async for``.  An\n"
            "``AsyncBaseSQLClient`` (an async SQLAlchemy engine) returns a plain\n"
            "sync iterator, and ``async for`` over it raises ``TypeError``.  Check\n"
            "which client produced the result first; for an async-engine client,\n"
            "suppress with a reason that names it.\n"
            "\n"
            "Matching is exact: the iterable is the ``await`` itself, or a local\n"
            "name assigned exactly once in the function, from that ``await``.  A\n"
            "result passed to a helper, stored, or rebound is not followed.  The\n"
            "check is silent unless the app's ``uv.lock`` resolves an SDK whose\n"
            "``get_batched_results`` result supports ``async for``.  See\n"
            + _APPS_DOC
            + ".\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P065]\n"
            "<reason>`` records a deliberate exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p065",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
)
