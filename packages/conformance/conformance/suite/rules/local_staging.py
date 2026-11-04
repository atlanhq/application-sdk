"""Local-staging rule definitions (P063).

A connector stages the files it writes during a run on local disk before the SDK
uploads them.  The SDK's writable scratch prefix is ``TEMPORARY_PATH``; the
container working directory is commonly read-only on a customer-hosted runtime.
A path built from the object-store key template and never rooted under
``TEMPORARY_PATH`` is therefore relative to that read-only directory, and the
connector passes preflight (nothing is written yet) and then fails every fetch
activity.

P-ids are a permanent public contract (see ``prescriptions.py``).
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

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P063",
        canonical_reference=(
            "application_sdk/templates/sql_app.py — `SqlApp.run()` wraps "
            "`WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...)` in "
            "`os.path.join(TEMPORARY_PATH, ...)` and hands that rooted path to "
            "every extract and transform task; `get_object_store_prefix()` strips "
            "the root back off, so the object-store key is unchanged."
        ),
        scope=RuleScope.APP,
        name="LocalStagingOutsideTemporaryPath",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="local-staging",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.55.0",
        rationale=(
            "WORKFLOW_OUTPUT_PATH_TEMPLATE expands to an object-store key "
            "('artifacts/apps/<app>/workflows/<workflow>/<run>'). Used as a local "
            "directory it resolves against the container working directory, which "
            "is commonly read-only on a customer-hosted runtime. The connector "
            "passes preflight, because nothing has been written yet, and then every "
            "fetch activity fails with LocalVolumeUnwritableError. The SDK's "
            "writable scratch prefix is TEMPORARY_PATH: root every local path built "
            "from the key there."
        ),
        short_description=(
            "Local staging path built from WORKFLOW_OUTPUT_PATH_TEMPLATE without a "
            "TEMPORARY_PATH root"
        ),
        full_description=(
            "Flags a ``WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...)`` call whose result\n"
            "is not rooted under ``TEMPORARY_PATH``.  The template is an\n"
            "object-store key; a connector that writes its extract files under it\n"
            "writes relative to the container working directory, which is read-only\n"
            "on many customer-hosted runtimes.\n"
            "\n"
            "The call is rooted, and not flagged, when an enclosing expression also\n"
            "names ``TEMPORARY_PATH`` (``os.path.join(TEMPORARY_PATH, ...)``), when\n"
            "it is bound to a local that a same-function expression joins with\n"
            "``TEMPORARY_PATH``, or when it is returned from a helper whose every\n"
            "call in the module is rooted.  A value passed straight to\n"
            "``get_object_store_prefix`` / ``normalize_key`` or to a ``storage_path=``\n"
            "keyword is an object-store key, not a local path, and is not flagged.\n"
            "A literal ``artifacts/...`` string is not matched: apps hold object-store\n"
            "keys with that spelling that never touch local disk.\n"
            "\n"
            "Fix: ``os.path.join(TEMPORARY_PATH, WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...))``\n"
            "(``from application_sdk.constants import TEMPORARY_PATH``).  Object-store\n"
            "calls keep receiving the key, because ``get_object_store_prefix`` and\n"
            "``normalize_key`` strip ``TEMPORARY_PATH`` back off.\n"
            "\n"
            "Suppress a value that is only ever an object-store key with a\n"
            "justification: ``# conformance: ignore[P063] <reason>``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
            "conformance/docs/rules/prescriptions.md#p063"
        ),
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
)
