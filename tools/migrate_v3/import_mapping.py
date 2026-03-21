"""
Single source of truth for all v2 → v3 import path rewrites.

Each entry describes how one deprecated import should be rewritten. The
rewriter uses SYMBOL_MAP first (exact module + symbol match) and falls back
to MODULE_MAP when the symbol isn't listed explicitly (e.g. for interceptors
whose symbol names are unchanged).

Entries with ``structural=True`` represent imports that, in addition to the
path change, require manual structural refactoring (e.g. merging a Workflow +
Activities pair into a single App subclass). The rewriter adds a
``# TODO(v3-migration)`` comment above those lines so reviewers know where
follow-up work is needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RewriteTarget:
    """Describes where an import should be rewritten to."""

    new_module: str
    # None means keep the original symbol name (used when the module path
    # changes but the exported name is the same).
    new_symbol: str | None
    # True = the import change alone is insufficient; structural refactoring
    # (class merges, API changes) is also required.
    structural: bool = False
    # Human-readable hint placed in the TODO comment.
    note: str = ""


# ---------------------------------------------------------------------------
# Symbol-level map  (old_module, old_symbol) → RewriteTarget
# ---------------------------------------------------------------------------
# Checked first. Covers all cases where the symbol name changes, is removed,
# or the mapping is ambiguous at the module level.

SYMBOL_MAP: dict[tuple[str, str], RewriteTarget] = {
    # ── Workflows → App ────────────────────────────────────────────────────
    ("application_sdk.workflows", "WorkflowInterface"): RewriteTarget(
        "application_sdk.app",
        "App",
        structural=True,
        note=(
            "Merge workflow + activities into a single App subclass with @task methods. "
            "See migration guide Step 1-3."
        ),
    ),
    # ── Activities → App ───────────────────────────────────────────────────
    ("application_sdk.activities", "ActivitiesInterface"): RewriteTarget(
        "application_sdk.app",
        "App",
        structural=True,
        note=(
            "Merge workflow + activities into a single App subclass with @task methods. "
            "See migration guide Step 1-3."
        ),
    ),
    # ── Handlers ───────────────────────────────────────────────────────────
    ("application_sdk.handlers", "HandlerInterface"): RewriteTarget(
        "application_sdk.handler",
        "Handler",
    ),
    ("application_sdk.handlers.base", "BaseHandler"): RewriteTarget(
        "application_sdk.handler.base",
        "DefaultHandler",
    ),
    ("application_sdk.handlers.sql", "BaseSQLHandler"): RewriteTarget(
        "application_sdk.handler",
        "Handler",
        structural=True,
        note=(
            "BaseSQLHandler removed — SQL logic absorbed into SqlMetadataExtractor. "
            "Change handler base class to Handler and move SQL class attributes to the "
            "SqlMetadataExtractor subclass. See migration guide Step 4."
        ),
    ),
    # ── Worker ─────────────────────────────────────────────────────────────
    ("application_sdk.worker", "Worker"): RewriteTarget(
        "application_sdk.execution",
        "create_worker",
        structural=True,
        note=(
            "Worker setup is now automatic via create_worker(). "
            "Remove manual workflow/activity registration — see migration guide Step 6."
        ),
    ),
    # ── Application entry-point ────────────────────────────────────────────
    ("application_sdk.application", "BaseApplication"): RewriteTarget(
        "application_sdk.main",
        "run_dev_combined",
        structural=True,
        note=(
            "Replace 'app = BaseApplication(...); await app.start()' with "
            "'asyncio.run(run_dev_combined(MyApp, handler_class=MyHandler))'. "
            "See migration guide Step 5."
        ),
    ),
    (
        "application_sdk.application.metadata_extraction.sql",
        "BaseSQLMetadataExtractionApplication",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlMetadataExtractor",
        structural=True,
        note=(
            "Merge Application + Workflow + Activities into a single "
            "SqlMetadataExtractor subclass. See migration guide Step 1."
        ),
    ),
    # ── SQL template workflows ─────────────────────────────────────────────
    (
        "application_sdk.workflows.metadata_extraction.sql",
        "BaseSQLMetadataExtractionWorkflow",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlMetadataExtractor",
        structural=True,
        note=(
            "Merge workflow + activities into a single SqlMetadataExtractor subclass "
            "with @task methods. See migration guide Step 1."
        ),
    ),
    (
        "application_sdk.workflows.metadata_extraction",
        "MetadataExtractionWorkflow",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlMetadataExtractor",
        structural=True,
        note="Merge workflow + activities into SqlMetadataExtractor. See migration guide Step 1.",
    ),
    (
        "application_sdk.workflows.metadata_extraction.incremental_sql",
        "IncrementalSQLMetadataExtractionWorkflow",
    ): RewriteTarget(
        "application_sdk.templates",
        "IncrementalSqlMetadataExtractor",
        structural=True,
        note=(
            "Merge workflow + activities into a single IncrementalSqlMetadataExtractor subclass. "
            "See migration guide Step 3."
        ),
    ),
    (
        "application_sdk.workflows.query_extraction.sql",
        "SQLQueryExtractionWorkflow",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlQueryExtractor",
        structural=True,
        note=(
            "Merge workflow + activities into a single SqlQueryExtractor subclass. "
            "See migration guide Step 2."
        ),
    ),
    (
        "application_sdk.workflows.query_extraction",
        "QueryExtractionWorkflow",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlQueryExtractor",
        structural=True,
        note="Merge workflow + activities into SqlQueryExtractor. See migration guide Step 2.",
    ),
    # ── SQL template activities ────────────────────────────────────────────
    (
        "application_sdk.activities.metadata_extraction.sql",
        "BaseSQLMetadataExtractionActivities",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlMetadataExtractor",
        structural=True,
        note=(
            "Merge workflow + activities into a single SqlMetadataExtractor subclass. "
            "See migration guide Step 1."
        ),
    ),
    (
        "application_sdk.activities.metadata_extraction.base",
        "BaseMetadataExtractionActivities",
    ): RewriteTarget(
        "application_sdk.templates",
        "BaseMetadataExtractor",
        structural=True,
        note="Extend BaseMetadataExtractor or SqlMetadataExtractor directly.",
    ),
    (
        "application_sdk.activities.metadata_extraction.incremental",
        "BaseSQLIncrementalMetadataExtractionActivities",
    ): RewriteTarget(
        "application_sdk.templates",
        "IncrementalSqlMetadataExtractor",
        structural=True,
        note=(
            "Merge workflow + activities into a single IncrementalSqlMetadataExtractor subclass. "
            "See migration guide Step 3."
        ),
    ),
    (
        "application_sdk.activities.query_extraction.sql",
        "SQLQueryExtractionActivities",
    ): RewriteTarget(
        "application_sdk.templates",
        "SqlQueryExtractor",
        structural=True,
        note=(
            "Merge workflow + activities into a single SqlQueryExtractor subclass. "
            "See migration guide Step 2."
        ),
    ),
    # ── Activity models (renamed) ──────────────────────────────────────────
    ("application_sdk.activities.common.models", "ActivityStatistics"): RewriteTarget(
        "application_sdk.common.models",
        "TaskStatistics",
    ),
    ("application_sdk.activities.common.models", "ActivityResult"): RewriteTarget(
        "application_sdk.common.models",
        "TaskResult",
    ),
    # ── Auto-heartbeater (removed, built into @task) ───────────────────────
    (
        "application_sdk.activities.common.utils",
        "auto_heartbeater",
    ): RewriteTarget(
        "application_sdk.app",
        "task",
        structural=True,
        note=(
            "@auto_heartbeater is removed — heartbeating is built into "
            "@task(heartbeat_timeout_seconds=60, auto_heartbeat_seconds=10). "
            "Remove this import and the decorator. See migration guide Step 11."
        ),
    ),
    # ── Atlan clients ──────────────────────────────────────────────────────
    ("application_sdk.clients.atlan", "get_async_client"): RewriteTarget(
        "application_sdk.credentials.atlan_client",
        "create_async_atlan_client",
        structural=True,
        note=(
            "Signature changed: create_async_atlan_client(cred: AtlanApiToken). "
            "Use the credential system to supply the token. See migration guide Step 10."
        ),
    ),
    ("application_sdk.clients.atlan", "get_client"): RewriteTarget(
        "application_sdk.credentials.atlan_client",
        "create_async_atlan_client",
        structural=True,
        note=(
            "Sync get_client() is removed. Use async create_async_atlan_client() "
            "with AtlanApiToken credential. See migration guide Step 10."
        ),
    ),
    ("application_sdk.clients.atlan_auth", "AtlanAuthClient"): RewriteTarget(
        "application_sdk.credentials.oauth",
        "OAuthTokenService",
        structural=True,
        note=(
            "AtlanAuthClient replaced by OAuthTokenService. "
            "See migration guide Step 10."
        ),
    ),
    # ── Services (ObjectStore — no single class replacement) ───────────────
    ("application_sdk.services.objectstore", "ObjectStore"): RewriteTarget(
        "application_sdk.storage",
        # ObjectStore has no single replacement class; keep symbol name so the
        # ImportError surfaces alongside the TODO comment.
        "ObjectStore",
        structural=True,
        note=(
            "ObjectStore class removed. Use upload_file / download_file / list_keys / delete "
            "from application_sdk.storage, or self.context.* inside @task. "
            "See migration guide Step 8."
        ),
    ),
    ("application_sdk.services.secretstore", "SecretStore"): RewriteTarget(
        "application_sdk.infrastructure.secrets",
        "SecretStore",
    ),
    ("application_sdk.services.statestore", "StateStore"): RewriteTarget(
        "application_sdk.infrastructure.state",
        "StateStore",
    ),
    ("application_sdk.services.atlan_storage", "AtlanStorage"): RewriteTarget(
        "application_sdk.storage",
        "AtlanStorage",  # keep name; ImportError + TODO will guide the developer
        structural=True,
        note=(
            "AtlanStorage removed. Use App.upload() / App.download() or "
            "application_sdk.storage functions. See migration guide Step 8."
        ),
    ),
    # ── Test utilities ─────────────────────────────────────────────────────
    ("application_sdk.test_utils.credentials", "MockCredentialStore"): RewriteTarget(
        "application_sdk.testing",
        "MockCredentialStore",
    ),
}

# ---------------------------------------------------------------------------
# Module-level fallback map  old_module → (new_module, structural, note)
# ---------------------------------------------------------------------------
# Used when the symbol isn't in SYMBOL_MAP.  Symbol names are kept unchanged.

MODULE_MAP: dict[str, tuple[str, bool, str]] = {
    # Workflows
    "application_sdk.workflows": (
        "application_sdk.app",
        True,
        "Workflows replaced by App + @task. See migration guide Steps 1-3.",
    ),
    "application_sdk.workflows.metadata_extraction": (
        "application_sdk.templates",
        True,
        "Use SqlMetadataExtractor from application_sdk.templates.",
    ),
    "application_sdk.workflows.metadata_extraction.sql": (
        "application_sdk.templates",
        True,
        "Use SqlMetadataExtractor from application_sdk.templates.",
    ),
    "application_sdk.workflows.metadata_extraction.incremental_sql": (
        "application_sdk.templates",
        True,
        "Use IncrementalSqlMetadataExtractor from application_sdk.templates.",
    ),
    "application_sdk.workflows.query_extraction": (
        "application_sdk.templates",
        True,
        "Use SqlQueryExtractor from application_sdk.templates.",
    ),
    "application_sdk.workflows.query_extraction.sql": (
        "application_sdk.templates",
        True,
        "Use SqlQueryExtractor from application_sdk.templates.",
    ),
    # Activities
    "application_sdk.activities": (
        "application_sdk.app",
        True,
        "Activities replaced by App + @task. See migration guide Steps 1-3.",
    ),
    "application_sdk.activities.metadata_extraction.base": (
        "application_sdk.templates",
        True,
        "Use BaseMetadataExtractor from application_sdk.templates.",
    ),
    "application_sdk.activities.metadata_extraction.sql": (
        "application_sdk.templates",
        True,
        "Use SqlMetadataExtractor from application_sdk.templates.",
    ),
    "application_sdk.activities.metadata_extraction.incremental": (
        "application_sdk.templates",
        True,
        "Use IncrementalSqlMetadataExtractor from application_sdk.templates.",
    ),
    "application_sdk.activities.query_extraction.sql": (
        "application_sdk.templates",
        True,
        "Use SqlQueryExtractor from application_sdk.templates.",
    ),
    "application_sdk.activities.common.models": (
        "application_sdk.common.models",
        False,
        "",
    ),
    "application_sdk.activities.common.utils": (
        "application_sdk.execution._temporal.activity_utils",
        True,
        "auto_heartbeater removed — heartbeating built into @task. See migration guide Step 11.",
    ),
    "application_sdk.activities.common.sql_utils": (
        "application_sdk.common.sql_utils",
        False,
        "",
    ),
    "application_sdk.activities.lock_management": (
        "application_sdk.execution._temporal.lock_activities",
        False,
        "",
    ),
    # Handlers
    "application_sdk.handlers": (
        "application_sdk.handler",
        False,
        "",
    ),
    "application_sdk.handlers.base": (
        "application_sdk.handler.base",
        False,
        "",
    ),
    "application_sdk.handlers.sql": (
        "application_sdk.templates",
        True,
        "SQL handler logic absorbed into SqlMetadataExtractor. See migration guide Step 4.",
    ),
    # Application / worker
    "application_sdk.application": (
        "application_sdk.main",
        True,
        "Use run_dev_combined() or the CLI. See migration guide Step 5.",
    ),
    "application_sdk.application.metadata_extraction.sql": (
        "application_sdk.templates",
        True,
        "Merge Application + Workflow + Activities into SqlMetadataExtractor.",
    ),
    "application_sdk.worker": (
        "application_sdk.execution",
        True,
        "Worker setup is automatic via create_worker(). See migration guide Step 6.",
    ),
    # Services
    "application_sdk.services.objectstore": (
        "application_sdk.storage",
        True,
        "Use upload_file/download_file/list_keys/delete or self.context.* inside @task. Step 8.",
    ),
    "application_sdk.services.secretstore": (
        "application_sdk.infrastructure.secrets",
        False,
        "",
    ),
    "application_sdk.services.statestore": (
        "application_sdk.infrastructure.state",
        False,
        "",
    ),
    "application_sdk.services.eventstore": (
        "application_sdk.infrastructure",
        True,
        "Use get_infrastructure().event_binding — events are automatic via interceptor.",
    ),
    "application_sdk.services.atlan_storage": (
        "application_sdk.storage",
        True,
        "Use App.upload()/App.download() or application_sdk.storage functions. Step 8.",
    ),
    # Clients
    "application_sdk.clients.atlan": (
        "application_sdk.credentials.atlan_client",
        True,
        "Use create_async_atlan_client(AtlanApiToken). See migration guide Step 10.",
    ),
    "application_sdk.clients.atlan_auth": (
        "application_sdk.credentials.oauth",
        True,
        "AtlanAuthClient replaced by OAuthTokenService. See migration guide Step 10.",
    ),
    "application_sdk.clients.temporal": (
        "application_sdk.execution",
        False,
        "",
    ),
    "application_sdk.clients.workflow": (
        "application_sdk.execution",
        False,
        "",
    ),
    # Interceptors
    "application_sdk.interceptors.models": (
        "application_sdk.contracts.events",
        False,
        "",
    ),
    "application_sdk.interceptors.events": (
        "application_sdk.interceptors.events",
        True,
        "Remove this import — interceptors are auto-registered by create_worker() in v3.",
    ),
    "application_sdk.interceptors.cleanup": (
        "application_sdk.interceptors.cleanup",
        True,
        "Remove this import — interceptors are auto-registered by create_worker() in v3.",
    ),
    "application_sdk.interceptors.lock": (
        "application_sdk.interceptors.lock",
        True,
        "Remove this import — interceptors are auto-registered by create_worker() in v3.",
    ),
    "application_sdk.interceptors.correlation_context": (
        "application_sdk.interceptors.correlation_context",
        True,
        "Remove this import — interceptors are auto-registered by create_worker() in v3.",
    ),
    "application_sdk.interceptors.activity_failure_logging": (
        "application_sdk.interceptors.activity_failure_logging",
        True,
        "Remove this import — interceptors are auto-registered by create_worker() in v3.",
    ),
    # Test utilities
    "application_sdk.test_utils": (
        "application_sdk.testing",
        False,
        "",
    ),
    "application_sdk.test_utils.credentials": (
        "application_sdk.testing",
        False,
        "",
    ),
    "application_sdk.test_utils.scale_data_generator": (
        "application_sdk.testing.scale_data_generator",
        False,
        "",
    ),
}

# Convenience set — used by the rewriter to quickly skip non-deprecated modules.
DEPRECATED_MODULES: frozenset[str] = frozenset(MODULE_MAP)
