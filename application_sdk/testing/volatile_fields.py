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

Empirical basis for the run-volatile set: three connector golden-corpus efforts
independently converged on exactly these three keys. One records that its
transformer emits these and no ``__timestamp`` and no ``guid``; another strips
these plus any key containing ``__timestamp``; a third emits none at all
because its identifier is a deterministic uuid5. Connector-specific additions
beyond these three are passed per-call rather than added here.
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
