"""Canonical field-set definitions for metadata comparison.

Two distinct concepts were previously conflated across the SDK's comparison
engines, and they are not the same thing wearing two names.

**Run-volatile** (:data:`RUN_VOLATILE_FIELDS`) — fields whose value changes on
every extraction run of the same source against the same environment. A
differing value carries no information, so they must be stripped before *any*
comparison, whatever its purpose.

**Environment-scoped** (:data:`ENVIRONMENT_SCOPED_FIELDS` and
:data:`ENVIRONMENT_SCOPED_NESTED_FIELDS`) — fields whose value is stable across
runs but differs when the same source is extracted against a *different*
tenant, connection, or connector instance. Whether they are noise depends
entirely on what is being compared:

* Comparing an extraction against a baseline captured on *different*
  infrastructure (the integration-test case), these must be ignored — the
  connection name and every ``qualifiedName`` derived from it legitimately
  differ, and reporting them would bury the real diffs.
* Comparing an extraction against a golden fixture captured from the *same*
  environment and re-baselined in place (the golden-corpus case), these must be
  **kept**. ``qualifiedName`` is the comparison key there, and a differing
  ``qualifiedName`` is a real regression in qualified-name construction — one of
  the highest-value bugs the diff can catch. Ignoring it would be
  self-defeating.

Which is why there is one documented constant per concept rather than one
merged list: consumers compose the sets their own comparison actually needs.

The run-volatile set is the intersection of what the SDK's two comparison
engines (:mod:`application_sdk.testing.integration.comparison` and
:mod:`application_sdk.testing.parity.comparator`) each stripped before they
shared this module, and it agrees with what the connector suites independently
found: MicroStrategy's transformer emits exactly these three keys and no
``__timestamp`` and no ``guid``; NetSuite's strips these three plus any key
containing ``__timestamp``; PowerCenter's transform emits none of them at all,
because its ``id`` is a deterministic uuid5. Three sources, no fourth field —
which is why the set is fixed here rather than negotiated per connector.
Connector-specific additions (NetSuite's ``__timestamp`` suffix rule, say) are
passed per-call rather than added here.
"""

RUN_VOLATILE_FIELDS: frozenset[str] = frozenset(
    {
        "lastSyncRun",
        "lastSyncRunAt",
        "lastSyncWorkflowName",
    }
)
"""Fields that change on every run. Strip before any comparison."""

ENVIRONMENT_SCOPED_FIELDS: frozenset[str] = frozenset(
    {
        "connectionName",
        "connectionQualifiedName",
        "databaseQualifiedName",
        "qualifiedName",
        "schemaQualifiedName",
        "tableQualifiedName",
        "tenantId",
        "viewQualifiedName",
    }
)
"""Fields that differ between environments but are stable across runs.

Ignore when the baseline came from other infrastructure; keep when comparing
against a golden fixture captured from the same environment.
"""

ENVIRONMENT_SCOPED_NESTED_FIELDS: frozenset[str] = frozenset(
    {
        "atlanSchema",
        "database",
        "materialisedView",
        "parentTable",
        "table",
        "tablePartition",
        "view",
    }
)
"""Attributes holding nested reference objects with environment-scoped contents.

These name whole reference *attributes* rather than leaf keys: the objects they
contain carry qualified names throughout, so environment-scoped comparison
skips the attribute entirely instead of descending into it.
"""

__all__ = [
    "ENVIRONMENT_SCOPED_FIELDS",
    "ENVIRONMENT_SCOPED_NESTED_FIELDS",
    "RUN_VOLATILE_FIELDS",
]
