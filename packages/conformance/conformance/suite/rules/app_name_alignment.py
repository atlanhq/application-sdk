"""P025 AppNameContractCodeDrift — app-name alignment rule (BLDX-1491).

Three independent sources declare an app's name, and they drift silently:

1. **Code (authoritative):** the ``App`` subclass (or an SDK template subclass
   such as ``SqlMetadataExtractor``).  The SDK derives the name in
   ``App.__init_subclass__`` as ``cls.name or _pascal_to_kebab(cls.__name__)``,
   so an explicit ``name = "..."`` wins; otherwise the class name is kebab-cased.

2. **Contract (toolkit-owned):** ``atlan.yaml`` top-level ``name:`` — generated
   from ``contract/app.pkl`` ``name``.  This value becomes the
   ``ATLAN_APPLICATION_NAME`` env var at deploy and drives the task-queue prefix,
   ``workflow_type``, and log-identity tags.

3. **Env var:** ``ATLAN_APPLICATION_NAME=...``, committed in ``.env.example``,
   read at runtime into ``constants.APPLICATION_NAME`` (defaults silently to
   ``"default"`` when unset, which routes every artifact write to
   ``artifacts/apps/default/...``).

``poe generate`` only runs ``pkl eval`` and never imports app code, so the three
can drift silently.  When code and contract disagree, PR #2380 (BLDX-1491) fixed
the runtime symptom by having ``App.upload()`` prefer the class-derived
``_app_name`` over the env var.  This rule closes the static side: it catches the
drift in CI before the app ships.

The MSSQL incident: ``MSSQLMetadataExtractor(SqlMetadataExtractor)`` had no
explicit ``name``, so ``_app_name`` defaulted to ``mssql-metadata-extractor``
(kebab class name).  But ``atlan.yaml`` and ``.env.example`` said ``mssql``.
At runtime, ``App.upload()`` wrote to ``artifacts/apps/mssql-metadata-extractor/…``
while the publish path was derived from ``APPLICATION_NAME`` (``mssql``), causing
publish to find 0 files and the workflow to stall.

Scope
-----
``APP`` only: consumer apps have a contract and an ``atlan.yaml``; the SDK itself
does not.
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
        id="P025",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — `MySQLApp` declares `name: ClassVar[str] = "
            '"mysql"`, atlan.yaml carries `name: mysql` and .env.example sets '
            "`ATLAN_APPLICATION_NAME=mysql`, so the three sources this rule compares "
            "agree. The task queue and the artifact path are derived from that name, "
            "so any disagreement routes work to a queue no worker is listening on."
        ),
        scope=RuleScope.APP,
        name="AppNameContractCodeDrift",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="app-name-alignment",
        autofixable=False,
        since="0.9.0",
        rationale=(
            "Three independent sources declare an app's name: the App subclass "
            "(code, authoritative), atlan.yaml top-level name: (contract), and "
            "ATLAN_APPLICATION_NAME in .env.example (env).  When they diverge, "
            "extract and publish write/read at different artifact paths, leaving "
            "0 files for publish to find and stalling the workflow (BLDX-1491, "
            "observed in the MSSQL connector). "
            "Customer impact: the customer's crawl extracts successfully and then "
            "publishes nothing — zero assets appear in their catalog while the workflow "
            "stalls or reports progress, the same silent-zero-asset class P030 polices "
            "at the upload seam."
        ),
        short_description=(
            "App name in code, atlan.yaml, or .env.example do not agree"
        ),
        full_description=(
            "Three independent sources declare an app's name:\n"
            "\n"
            "1. **Code (authoritative):** the ``App`` subclass (or an SDK template"
            " subclass).  The SDK derives the name as"
            " ``cls.name or _pascal_to_kebab(cls.__name__)``: an explicit"
            ' ``name = "..."`` wins; otherwise the PascalCase class name is'
            " kebab-cased (``MSSQLMetadataExtractor`` → ``mssql-metadata-extractor``).\n"
            "\n"
            "2. **Contract:** ``atlan.yaml`` top-level ``name:`` (generated from"
            " ``contract/app.pkl``).  This value becomes ``ATLAN_APPLICATION_NAME``"
            " at deploy and drives the task-queue prefix, ``workflow_type``, and"
            " log-identity tags.\n"
            "\n"
            "3. **Env:** ``ATLAN_APPLICATION_NAME=...`` in ``.env.example``,"
            " read at runtime into ``constants.APPLICATION_NAME``"
            ' (defaults silently to ``"default"`` when unset).\n'
            "\n"
            "``poe generate`` runs ``pkl eval`` but never imports app code, so"
            " the three can drift silently.  Drift causes artifact-path"
            " mismatches: ``App.upload()`` writes to"
            " ``artifacts/apps/<code-name>/…`` while publish reads"
            " ``artifacts/apps/<contract-name>/…``, finding 0 files (BLDX-1491).\n"
            "\n"
            "**This rule checks (per repo):**\n"
            "\n"
            "* **Code vs contract:** the code-derived app name must equal the"
            " ``atlan.yaml`` top-level ``name:`` value.\n"
            "\n"
            "* **Code vs env:** the code-derived app name must equal"
            " ``ATLAN_APPLICATION_NAME`` in ``.env.example``.\n"
            "\n"
            "* **Non-literal ``name =``:** a class-level ``name = variable``"
            " that cannot be statically resolved is flagged as unverifiable.\n"
            "\n"
            "**No-op when:**\n"
            "\n"
            "* No App-family leaf class is found in the Python source (SDK repos,"
            " utility libraries).\n"
            "* No ``atlan.yaml`` and no ``.env.example`` are present (non-native-app"
            " repos).\n"
            "* Multiple App-family leaf classes with different names are found"
            " (bundle repos — handled by P016 instead).\n"
            "\n"
            "**Remediation**\n"
            "\n"
            "The code-derived name is the source of truth (mirrors"
            " ``AppRegistry.resolve_running_app_name`` from PR #2380).  To fix"
            " drift:\n"
            "\n"
            '1. Add an explicit ``name = \\"<intended-name>\\"`` class variable to the'
            " App subclass so the code name is unambiguous.  If ``<intended-name>``"
            " differs from the name the class resolves to today, this renames"
            " ``App.name``, and ``App.name`` is also the Temporal workflow type the"
            " worker registers (``<app>`` for the implicit ``run()``,"
            " ``<app>:<entry-point>`` for each ``@entrypoint``).  Live DAGs still"
            " dispatch the old type, so the rename requires an alias that keeps it"
            ' dispatchable: ``legacy_workflow_types = {"<old-type>":'
            ' "<entry-point>"}`` on the App subclass (``run`` for the implicit'
            " entry point), and the same pair in ``legacyWorkflowTypes`` in"
            " ``contract/app.pkl`` (K015 holds the two in agreement).  Without the"
            " alias, no worker claims the old type and live DAGs are stranded.\n"
            "\n"
            "2. Update ``contract/app.pkl`` ``name`` to match and re-run"
            " ``pkl eval`` (never hand-edit ``atlan.yaml`` — C002 catches stale"
            " generated artifacts).\n"
            "\n"
            "3. Update ``ATLAN_APPLICATION_NAME`` in ``.env.example`` to match"
            " the code name.\n"
            "\n"
            "Suppress with ``# conformance: ignore[P025] <reason>`` on the App"
            " class definition line (or the comment-only line directly above it)"
            " when a deliberate mismatch is justified.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p025"
        ),
        remediation_reference=RemediationReference(
            kind=RemediationKind.DECISION,
            target="app owner",
            note=(
                "align the contract and .env.example to the code name, or rename "
                "App.name; a rename changes the Temporal workflow type, so the old "
                "type must stay dispatchable through a legacy_workflow_types alias, "
                "else live DAGs are stranded"
            ),
        ),
    ),
)
